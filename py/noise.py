from __future__ import annotations

import abc
import functools as fun
import operator as op
from typing import Callable

import comfy
import torch
from comfy.k_diffusion import sampling
from torch import Tensor

from .noise_generation import *

# ruff: noqa: D412, D413, D417, D212, D407, ANN002, ANN003, FBT001, FBT002, S311


class CustomNoiseItemBase(abc.ABC):
    def __init__(self, factor, **kwargs):
        self.factor = factor
        self.keys = set(kwargs.keys())
        for k, v in kwargs.items():
            setattr(self, k, v)

    def clone(self):
        return self.__class__(self.factor, **{k: getattr(self, k) for k in self.keys})

    def set_factor(self, factor):
        self.factor = factor
        return self

    @abc.abstractmethod
    def make_noise_sampler(
        self,
        x: Tensor,
        sigma_min=None,
        sigma_max=None,
        seed=None,
        cpu=True,
        normalized=True,
    ):
        raise NotImplementedError


class CustomNoiseItem(CustomNoiseItemBase):
    def __init__(self, factor, **kwargs):
        super().__init__(factor, **kwargs)
        if getattr(self, "noise_type", None) is None:
            raise ValueError("Noise type required!")

    @torch.no_grad()
    def make_noise_sampler(
        self,
        x: Tensor,
        sigma_min=None,
        sigma_max=None,
        seed=None,
        cpu=True,
        normalized=True,
    ):
        return get_noise_sampler(
            self.noise_type,
            x,
            sigma_min,
            sigma_max,
            seed=seed,
            cpu=cpu,
            factor=self.factor,
            normalized=normalized,
        )


class CustomNoiseChain:
    def __init__(self, items=None):
        self.items = items if items is not None else []

    def clone(self):
        return CustomNoiseChain(
            [i.clone() for i in self.items],
        )

    def add(self, item):
        if item is None:
            raise ValueError("Attempt to add nil item")
        self.items.append(item)

    @property
    def factor(self):
        return sum(abs(i.factor) for i in self.items)

    def rescaled(self, scale=1.0):
        divisor = self.factor / scale
        divisor = divisor if divisor != 0 else 1.0
        result = self.clone()
        if divisor != 1:
            for i in result.items:
                i.set_factor(i.factor / divisor)
        return result

    @torch.no_grad()
    def make_noise_sampler(
        self,
        x: Tensor,
        sigma_min=None,
        sigma_max=None,
        seed=None,
        cpu=True,
        normalized=True,
    ) -> Callable:
        noise_samplers = tuple(
            i.make_noise_sampler(
                x,
                sigma_min,
                sigma_max,
                seed=seed,
                cpu=cpu,
                normalized=False,
            )
            for i in self.items
        )
        if not noise_samplers or not all(noise_samplers):
            raise ValueError("Failed to get noise sampler")
        factor = self.factor

        def noise_sampler(sigma, sigma_next):
            result = fun.reduce(
                op.add,
                (ns(sigma, sigma_next) for ns in noise_samplers),
            )
            if normalized:
                return scale_noise(result, factor)
            return result.mul_(factor)

        return noise_sampler


class NoiseSampler:
    def __init__(
        self,
        x: Tensor,
        sigma_min: float | None = None,
        sigma_max: float | None = None,
        seed: int | None = None,
        cpu: bool = False,
        transform: Callable = lambda t: t,
        make_noise_sampler: Callable | None = None,
        normalized=False,
        factor: float = 1.0,
    ):
        try:
            self.noise_sampler = make_noise_sampler(
                x,
                transform(torch.as_tensor(sigma_min))
                if sigma_min is not None
                else None,
                transform(torch.as_tensor(sigma_max))
                if sigma_max is not None
                else None,
                seed=seed,
                cpu=cpu,
            )
        except TypeError:
            self.noise_sampler = make_noise_sampler(x)
        self.factor = factor
        self.normalized = normalized
        self.transform = transform
        self.device = x.device
        self.dtype = x.dtype

    @classmethod
    def simple(cls, f):
        return lambda *args, **kwargs: cls(
            *args,
            **kwargs,
            make_noise_sampler=lambda x, *_args, **_kwargs: lambda _s, _sn: f(x),
        )

    @classmethod
    def wrap(cls, f):
        return lambda *args, **kwargs: cls(*args, **kwargs, make_noise_sampler=f)

    def __call__(self, *args, **kwargs):
        args = (
            self.transform(torch.as_tensor(s)) if s is not None else s for s in args
        )
        noise = self.noise_sampler(*args, **kwargs)
        noise = (
            scale_noise(noise, self.factor)
            if self.normalized
            else noise.mul_(self.factor)
        )
        if hasattr(noise, "to"):
            noise = noise.to(dtype=self.dtype, device=self.device)
        return noise


class CompositeNoise:
    def __init__(self, factor, dst, src, normalize_src, normalize_dst, mask):
        self.factor = factor
        self.dst_noise_sampler = dst
        self.src_noise_sampler = src
        self.normalize_src = normalize_src
        self.normalize_dst = normalize_dst
        self.mask = mask

    def clone(self):
        return CompositeNoise(
            self.factor,
            self.dst_noise_sampler,
            self.src_noise_sampler,
            self.normalize_src,
            self.normalize_dst,
            self.mask.clone(),
        )

    def set_factor(self, factor):
        self.factor = factor
        return self

    def make_noise_sampler(self, x, *args, normalized=True, **kwargs):
        normalize_src = (
            self.normalize_src if self.normalize_src is not None else normalized
        )
        normalize_dst = (
            self.normalize_dst if self.normalize_dst is not None else normalized
        )
        nsd = self.dst_noise_sampler(x, *args, normalized=False, **kwargs)
        nss = self.src_noise_sampler(x, *args, normalized=False, **kwargs)
        mask = self.mask.to(x.device, copy=True)
        mask = torch.nn.functional.interpolate(
            mask.reshape((-1, 1, *mask.shape[-2:])),
            size=x.shape[-2:],
            mode="bilinear",
        )
        mask = comfy.utils.repeat_to_batch_size(mask, x.shape[0])
        imask = torch.ones_like(mask) - mask

        def noise_sampler(s, sn):
            noise_dst = scale_noise(
                nsd(s, sn),
                self.factor,
                normalized=normalize_dst,
            ).mul_(
                imask,
            )
            noise_src = scale_noise(
                nss(s, sn),
                self.factor,
                normalized=normalize_src,
            ).mul_(mask)
            return noise_dst.add_(noise_src)

        return noise_sampler


class GuidedNoise:
    def __init__(
        self,
        factor,
        guidance_factor,
        ref_latent,
        noise_sampler,
        method,
        normalize,
        normalize_ref,
    ):
        self.factor = factor
        self.normalize = normalize
        self.normalize_ref = normalize_ref
        self.ref_latent = ref_latent
        self.noise_sampler = noise_sampler
        self.method = method
        self.guidance_factor = guidance_factor

    def clone(self):
        return GuidedNoise(
            self.factor,
            self.guidance_factor,
            self.ref_latent.clone(),
            self.noise_sampler,
            self.method,
            self.normalize,
            self.normalize_ref,
        )

    def set_factor(self, factor):
        self.factor = factor
        return self

    def make_noise_sampler(self, x, *args, normalized=True, **kwargs):
        from .sonar import SonarGuidanceMixin

        normalize = self.normalize if self.normalize is not None else normalized
        ns = self.noise_sampler(x, *args, normalized=False, **kwargs)
        ref_latent = scale_noise(
            self.ref_latent.to(x, copy=True),
            normalized=self.normalize_ref,
        )
        match self.method:
            case "linear":

                def noise_sampler(s, sn):
                    return scale_noise(
                        SonarGuidanceMixin.guidance_linear(
                            scale_noise(ns(s, sn), normalized=normalize),
                            ref_latent,
                            self.guidance_factor,
                        ),
                        self.factor,
                        normalized=normalize,
                    )
            case "euler":

                def noise_sampler(s, sn):
                    return scale_noise(
                        SonarGuidanceMixin.guidance_euler(
                            s,
                            sn,
                            scale_noise(ns(s, sn), normalized=normalize),
                            x,
                            ref_latent,
                            self.guidance_factor,
                        ),
                        self.factor,
                        normalized=normalize,
                    )

        return noise_sampler


class ScheduledNoise:
    def __init__(
        self,
        factor,
        noise_sampler,
        start_sigma,
        end_sigma,
        normalize,
        fallback_noise_sampler=None,
    ):
        self.factor = factor
        self.noise_sampler = noise_sampler
        self.start_sigma = start_sigma
        self.end_sigma = end_sigma
        self.normalize = normalize
        self.fallback_noise_sampler = fallback_noise_sampler

    def clone(self):
        return ScheduledNoise(
            self.factor,
            self.noise_sampler,
            self.start_sigma,
            self.end_sigma,
            self.normalize,
            fallback_noise_sampler=self.fallback_noise_sampler,
        )

    def set_factor(self, factor):
        self.factor = factor
        return self

    def make_noise_sampler(self, x, *args, normalized=True, **kwargs):
        normalize = self.normalize if self.normalize is not None else normalized
        ns = self.noise_sampler(x, *args, normalized=False, **kwargs)
        if self.fallback_noise_sampler:
            nsa = self.fallback_noise_sampler(x, *args, normalized=False, **kwargs)
        else:

            def nsa(_s, _sn):
                return torch.zeros_like(x)

        def noise_sampler(s, sn):
            if s <= self.start_sigma and s >= self.end_sigma:
                noise = ns(s, sn)
            else:
                noise = nsa(s, sn)
            return scale_noise(noise, self.factor, normalized=normalize)

        return noise_sampler


class RepeatedNoise:
    def __init__(self, factor, noise_sampler, repeat_length, normalize, permute=True):
        self.factor = factor
        self.normalize = normalize
        self.noise_sampler = noise_sampler
        self.repeat_length = repeat_length
        self.permute = permute

    def clone(self):
        return RepeatedNoise(
            self.factor,
            self.noise_sampler,
            self.repeat_length,
            self.normalize,
            self.permute,
        )

    def set_factor(self, factor):
        self.factor = factor
        return self

    def make_noise_sampler(self, x, *args, normalized=True, **kwargs):
        normalize = self.normalize if self.normalize is not None else normalized
        ns = self.noise_sampler(x, *args, normalized=False, **kwargs)
        noise_items = []
        permute_options = 2
        u32_max = 0xFFFF_FFFF
        seed = kwargs.get("seed")
        if seed is None:
            seed = torch.randint(
                -u32_max,
                u32_max,
                (1,),
                device="cpu",
                dtype=torch.int64,
            ).item()
        gen = torch.Generator(device="cpu")
        gen.manual_seed(seed)

        def noise_sampler(s, sn):
            rands = torch.randint(
                u32_max,
                (4,),
                generator=gen,
                dtype=torch.uint32,
            ).tolist()
            if len(noise_items) < self.repeat_length:
                idx = len(noise_items)
                noise_items.append(ns(s, sn))
            else:
                idx = rands[0] % self.repeat_length
            noise = noise_items[idx]
            if not self.permute:
                return noise.clone()
            noise_dims = len(noise.shape)
            match rands[1] % permute_options:
                case 0:
                    if rands[2] <= u32_max // 10:
                        # 10% of the time we return the original tensor instead of flipping
                        noise = noise.clone()
                    else:
                        dim = -1 + (rands[2] % (noise_dims + 1))
                        noise = torch.flip(noise, (dim,))
                case 1:
                    dim = rands[2] % noise_dims
                    count = rands[3] % noise.shape[dim]
                    noise = torch.roll(noise, count, dims=(dim,)).clone()
            return scale_noise(noise, self.factor, normalized=normalize)

        return noise_sampler


# Modulated noise functions copied from https://github.com/Clybius/ComfyUI-Extra-Samplers
# They probably don't work correctly for normal sampling.
class ModulatedNoise:
    MODULATION_DIMS = (-3, (-2, -1), (-3, -2, -1))

    def __init__(
        self,
        factor,
        noise_sampler,
        normalize_result,
        normalize_noise,
        normalize_ref,
        modulation_type="none",
        modulation_strength=2.0,
        modulation_dims=3,
        ref_latent_opt=None,
    ):
        self.factor = factor
        self.normalize_result = normalize_result
        self.normalize_noise = normalize_noise
        self.normalize_ref = normalize_ref
        self.noise_sampler = noise_sampler
        self.modulation_dims = modulation_dims
        self.type = modulation_type
        self.strength = modulation_strength
        self.ref_latent_opt = ref_latent_opt
        match self.type:
            case "intensity":
                self.modulation_function = self.intensity_based_multiplicative_noise
            case "frequency":
                self.modulation_function = self.frequency_based_noise
            case "spectral_signum":
                self.modulation_function = self.spectral_modulate_noise
            case _:
                self.modulation_function = None

    def clone(self):
        return ModulatedNoise(
            self.factor,
            self.noise_sampler,
            self.normalize_result,
            self.normalize_noise,
            self.normalize_ref,
            self.type,
            self.strength,
            self.modulation_dims,
            self.ref_latent_opt,
        )

    def set_factor(self, factor):
        self.factor = factor
        return self

    def make_noise_sampler(self, x, *args, normalized=True, **kwargs):
        normalize_result = (
            self.normalize_result if self.normalize_result is not None else normalized
        )
        normalize_noise = (
            self.normalize_noise if self.normalize_noise is not None else normalized
        )
        dims = self.MODULATION_DIMS[self.modulation_dims - 1]
        ns = self.noise_sampler(x, *args, **kwargs)
        if not self.modulation_function:

            def noise_sampler(s, sn):
                return scale_noise(
                    ns(s, sn),
                    self.factor,
                    normalized=normalize_result or normalize_noise,
                )

            return noise_sampler

        ref_latent = None
        if self.ref_latent_opt is not None:
            ref_latent = self.ref_latent_opt.to(x, copy=True)

        def noise_sampler(s, sn):
            noise = self.modulation_function(
                scale_noise(
                    x if self.ref_latent_opt is None else ref_latent,
                    normalized=self.normalize_ref,
                ),
                scale_noise(ns(s, sn), normalized=normalize_noise),
                1.0,  # s_noise
                1.0,  # sigma_up
                self.strength,
                dims,
            )
            return scale_noise(noise, self.factor, normalized=normalize_result)

        return noise_sampler

    @staticmethod
    def intensity_based_multiplicative_noise(
        x,
        noise,
        s_noise,
        sigma_up,
        intensity,
        dims,
    ) -> torch.Tensor:
        """Scales noise based on the intensities of the input tensor."""
        std = torch.std(
            x - x.mean(),
            dim=dims,
            keepdim=True,
        )  # Average across channels to get intensity
        scaling = (
            1 / (std * abs(intensity) + 1.0)
        )  # Scale std by intensity, as not doing this leads to more noise being left over, leading to crusty/preceivably extremely oversharpened images
        additive_noise = noise * s_noise * sigma_up
        scaled_noise = noise * s_noise * sigma_up * scaling + additive_noise

        noise_norm = torch.norm(additive_noise)
        scaled_noise_norm = torch.norm(scaled_noise)
        scaled_noise *= noise_norm / scaled_noise_norm  # Scale to normal noise strength
        return scaled_noise * intensity + additive_noise * (1 - intensity)

    @staticmethod
    def frequency_based_noise(
        z_k,
        noise,
        s_noise,
        sigma_up,
        intensity,
        channels,
    ) -> torch.Tensor:
        """Scales the high-frequency components of the noise based on the given intensity."""
        additive_noise = noise * s_noise * sigma_up

        std = torch.std(
            z_k - z_k.mean(),
            dim=channels,
            keepdim=True,
        )  # Average across channels to get intensity
        scaling = 1 / (std * abs(intensity) + 1.0)
        # Perform Fast Fourier Transform (FFT)
        z_k_freq = torch.fft.fft2(scaling * additive_noise + additive_noise)

        # Get the magnitudes of the frequency components
        magnitudes = torch.abs(z_k_freq)

        # Create a high-pass filter (emphasize high frequencies)
        h, w = z_k.shape[-2:]
        b = abs(
            intensity,
        )  # Controls the emphasis of the high pass (higher frequencies are boosted)
        high_pass_filter = 1 - torch.exp(
            -((torch.arange(h)[:, None] / h) ** 2 + (torch.arange(w)[None, :] / w) ** 2)
            * b**2,
        )
        high_pass_filter = high_pass_filter.to(z_k.device)

        # Apply the filter to the magnitudes
        magnitudes_scaled = magnitudes * (1 + high_pass_filter)

        # Reconstruct the complex tensor with scaled magnitudes
        z_k_freq_scaled = magnitudes_scaled * torch.exp(1j * torch.angle(z_k_freq))

        # Perform Inverse Fast Fourier Transform (IFFT)
        z_k_scaled = torch.fft.ifft2(z_k_freq_scaled)

        # Return the real part of the result
        z_k_scaled = torch.real(z_k_scaled)

        noise_norm = torch.norm(additive_noise)
        scaled_noise_norm = torch.norm(z_k_scaled)

        z_k_scaled *= noise_norm / scaled_noise_norm  # Scale to normal noise strength

        return z_k_scaled * intensity + additive_noise * (1 - intensity)

    @staticmethod
    def spectral_modulate_noise(
        _unused,
        noise,
        s_noise,
        sigma_up,
        intensity,
        channels,
        spectral_mod_percentile=5.0,
    ) -> torch.Tensor:  # Modified for soft quantile adjustment using a novel:tm::c::r: method titled linalg.
        additive_noise = noise * s_noise * sigma_up
        # Convert image to Fourier domain
        fourier = torch.fft.fftn(
            additive_noise,
            dim=channels,
        )  # Apply FFT along Height and Width dimensions

        log_amp = torch.log(torch.sqrt(fourier.real**2 + fourier.imag**2))

        quantile_low = (
            torch.quantile(
                log_amp.abs().flatten(1),
                spectral_mod_percentile * 0.01,
                dim=1,
            )
            .unsqueeze(-1)
            .unsqueeze(-1)
            .expand(log_amp.shape)
        )

        quantile_high = (
            torch.quantile(
                log_amp.abs().flatten(1),
                1 - (spectral_mod_percentile * 0.01),
                dim=1,
            )
            .unsqueeze(-1)
            .unsqueeze(-1)
            .expand(log_amp.shape)
        )

        quantile_max = (
            torch.quantile(log_amp.abs().flatten(1), 1, dim=1)
            .unsqueeze(-1)
            .unsqueeze(-1)
            .expand(log_amp.shape)
        )

        # Decrease high-frequency components
        mask_high = log_amp > quantile_high  # If we're larger than 95th percentile

        additive_mult_high = torch.where(
            mask_high,
            1
            - ((log_amp - quantile_high) / (quantile_max - quantile_high)).clamp_(
                max=0.5,
            ),  # (1) - (0-1), where 0 is 95th %ile and 1 is 100%ile
            torch.tensor(1.0),
        )

        # Increase low-frequency components
        mask_low = log_amp < quantile_low
        additive_mult_low = torch.where(
            mask_low,
            1
            + (1 - (log_amp / quantile_low)).clamp_(
                max=0.5,
            ),  # (1) + (0-1), where 0 is 5th %ile and 1 is 0%ile
            torch.tensor(1.0),
        )

        mask_mult = (additive_mult_low * additive_mult_high) ** intensity
        filtered_fourier = fourier * mask_mult

        # Inverse transform back to spatial domain
        inverse_transformed = torch.fft.ifftn(
            filtered_fourier,
            dim=channels,
        )  # Apply IFFT along Height and Width dimensions

        return inverse_transformed.real.to(additive_noise.device)


NOISE_SAMPLERS: dict[NoiseType, Callable] = {
    NoiseType.BROWNIAN: NoiseSampler.wrap(sampling.BrownianTreeNoiseSampler),
    NoiseType.GAUSSIAN: NoiseSampler.simple(torch.randn_like),
    NoiseType.UNIFORM: NoiseSampler.simple(uniform_noise_like),
    NoiseType.PERLIN: NoiseSampler.simple(rand_perlin_like),
    NoiseType.STUDENTT: NoiseSampler.simple(studentt_noise_like),
    NoiseType.PINK: NoiseSampler.simple(pink_noise_like),
    NoiseType.HIGHRES_PYRAMID: NoiseSampler.simple(highres_pyramid_noise_like),
    NoiseType.PYRAMID: NoiseSampler.simple(pyramid_noise_like),
    NoiseType.RAINBOW_MILD: NoiseSampler.simple(
        lambda x: (green_noise_like(x) * 0.55 + rand_perlin_like(x) * 0.7) * 1.15,
    ),
    NoiseType.RAINBOW_INTENSE: NoiseSampler.simple(
        lambda x: (green_noise_like(x) * 0.75 + rand_perlin_like(x) * 0.5) * 1.15,
    ),
    NoiseType.LAPLACIAN: NoiseSampler.simple(laplacian_noise_like),
    NoiseType.POWER: NoiseSampler.simple(power_noise_like),
    NoiseType.GREEN_TEST: NoiseSampler.simple(green_noise_like),
    NoiseType.PYRAMID_OLD: NoiseSampler.simple(pyramid_old_noise_like),
    NoiseType.PYRAMID_BISLERP: NoiseSampler.simple(
        lambda x: pyramid_noise_like(x, upscale_mode="bislerp"),
    ),
    NoiseType.HIGHRES_PYRAMID_BISLERP: NoiseSampler.simple(
        lambda x: highres_pyramid_noise_like(x, upscale_mode="bislerp"),
    ),
    NoiseType.PYRAMID_AREA: NoiseSampler.simple(
        lambda x: pyramid_noise_like(x, upscale_mode="area"),
    ),
    NoiseType.HIGHRES_PYRAMID_AREA: NoiseSampler.simple(
        lambda x: highres_pyramid_noise_like(x, upscale_mode="area"),
    ),
    NoiseType.PYRAMID_OLD_BISLERP: NoiseSampler.simple(
        lambda x: pyramid_old_noise_like(x, upscale_mode="bislerp"),
    ),
    NoiseType.PYRAMID_OLD_AREA: NoiseSampler.simple(
        lambda x: pyramid_old_noise_like(x, upscale_mode="area"),
    ),
}


def get_noise_sampler(
    noise_type: str | NoiseType | None,
    x: Tensor,
    sigma_min: float | None,
    sigma_max: float | None,
    seed: int | None = None,
    cpu: bool = True,
    factor: float = 1.0,
    normalized=False,
) -> Callable:
    if noise_type is None:
        noise_type = NoiseType.GAUSSIAN
    elif isinstance(noise_type, str):
        noise_type = NoiseType[noise_type.upper()]
    if noise_type == NoiseType.BROWNIAN and (sigma_min is None or sigma_max is None):
        raise ValueError("Must pass sigma min/max when using brownian noise")
    mkns = NOISE_SAMPLERS.get(noise_type)
    if mkns is None:
        raise ValueError("Unknown noise sampler")
    return mkns(
        x,
        sigma_min,
        sigma_max,
        seed=seed,
        cpu=cpu,
        factor=factor,
        normalized=normalized,
    )
