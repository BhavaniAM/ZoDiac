import numpy as np
from scipy.stats import norm
import scipy
import torch
import torch.nn.functional
from diffusers.utils.torch_utils import randn_tensor

class GTWatermark():
    def __init__(self, device, shape=(1,4,64,64), dtype=torch.float32, w_channel=3, w_radius=10, generator=None):
        self.device = device
        # from latent tensor
        self.shape = shape
        self.dtype = dtype
        # from hyperparameters
        self.w_channel = w_channel
        self.w_radius = w_radius

        self.gt_patch, self.watermarking_mask = self._gen_gt(generator=generator)
        self.mu, self.sigma = self.watermark_stat()

    def _circle_mask(self, size=64, r=10, x_offset=0, y_offset=0):
    # reference: https://stackoverflow.com/questions/69687798/generating-a-soft-circluar-mask-using-numpy-python-3
        x0 = y0 = size // 2
        x0 += x_offset
        y0 += y_offset
        y, x = np.ogrid[:size, :size]
        y = y[::-1]
        return ((x - x0)**2 + (y-y0)**2)<= r**2

    def _get_watermarking_pattern(self, gt_init): # in fft space
        gt_patch = torch.fft.fftshift(torch.fft.fft2(gt_init), dim=(-1, -2))
        for i in range(self.w_radius, 0, -1): # from outer circle to inner circle
            tmp_mask = torch.tensor(self._circle_mask(gt_init.shape[-1], r=i)).to(self.device) # circle mask in bool value
            gt_patch[:, self.w_channel, tmp_mask] = gt_patch[0, self.w_channel, 0, i].item() # set the value inside the circle to be a value from Gaussian Distribution
        return gt_patch

    def _get_watermarking_mask(self, gt_patch):
        watermarking_mask = torch.zeros(gt_patch.shape, dtype=torch.bool).to(self.device)
        watermarking_mask[:,self.w_channel] = torch.tensor(self._circle_mask(gt_patch.shape[-1], r=self.w_radius)).to(self.device)
        return watermarking_mask

    def _gen_gt(self, generator=None):
        gt_init = randn_tensor(self.shape, generator=generator, device=self.device, dtype=self.dtype)
        gt_patch = self._get_watermarking_pattern(gt_init)
        watermarking_mask = self._get_watermarking_mask(gt_patch)
        return gt_patch, watermarking_mask

    # def inject_watermark(self, latents): 
    #     latents_fft = torch.fft.fftshift(torch.fft.fft2(latents), dim=(-1, -2))
    #     if self.watermarking_mask.shape[-2:] != latents_fft.shape[-2:]:
    #         # Resize watermarking_mask and gt_patch to match latents_fft spatial dimensions
    #         mask_resized = torch.nn.functional.interpolate(
    #             self.watermarking_mask.float(), size=latents_fft.shape[-2:], mode='bilinear'
    #         ).bool()
    #         patch_resized = torch.nn.functional.interpolate(
    #             self.gt_patch, size=latents_fft.shape[-2:], mode='bilinear'
    #         )

    #     else:
    #         mask_resized = self.watermarking_mask
    #         patch_resized = self.gt_patch
        
    #     # latents_fft[self.watermarking_mask] = self.gt_patch[self.watermarking_mask].clone()
    #     latents_fft = latents_fft * (~mask_resized) + patch_resized * mask_resized
        
    #     print("Latents FFT shape:", latents_fft.shape)
    #     print("Mask shape:", self.watermarking_mask.shape)
    #     print("Patch shape:", self.gt_patch.shape)  

    #     latents_w = torch.fft.ifft2(torch.fft.ifftshift(latents_fft, dim=(-1, -2))).real
    #     return latents_w

    def inject_watermark(self, latents): 
        # Resize watermark mask and patch BEFORE applying FFT
        if self.watermarking_mask.shape[-2:] != latents.shape[-2:]:
            # Resize in real space
            mask_resized = torch.nn.functional.interpolate(
                self.watermarking_mask.float(), size=latents.shape[-2:], mode='bilinear'
            ).bool()
            # patch_resized = torch.nn.functional.interpolate(
            #     self.gt_patch, size=latents.shape[-2:], mode='bilinear'
            # )
            real_patch = torch.nn.functional.interpolate(self.gt_patch.real, size = latents.shape[-2:], mode = 'bilinear')
            imag_patch = torch.nn.functional.interpolate(self.gt_patch.imag, size = latents.shape[-2:], mode = 'bilinear')
            patch_resized = torch.complex(real_patch, imag_patch)
        else:
            mask_resized = self.watermarking_mask
            patch_resized = self.gt_patch

        self.mask_resized = mask_resized  # Save resized mask
        self.patch_resized = patch_resized  # Save resized patch

        # FFT on the latents
        latents_fft = torch.fft.fftshift(torch.fft.fft2(latents), dim=(-1, -2))
    
        # Apply watermark in the frequency domain
        latents_fft = latents_fft * (~mask_resized) + patch_resized * mask_resized
    
        # Inverse FFT to return to image space (take real part)
        latents_w = torch.fft.ifft2(torch.fft.ifftshift(latents_fft, dim=(-1, -2))).real
    
        # Debug prints
        print("Latents FFT shape:", latents_fft.shape)
        print("Mask resized shape:", mask_resized.shape)
        print("Patch resized shape:", patch_resized.shape)  
    
        return latents_w


    def eval_watermark(self, latents_w):
        latents_w_fft = torch.fft.fftshift(torch.fft.fft2(latents_w), dim=(-1, -2))
        l1_metric = torch.abs(latents_w_fft[self.watermarking_mask] - self.gt_patch[self.watermarking_mask]).mean().item()
        return l1_metric

    def watermark_stat(self):
        dis_all = []
        for i in range(1000):
            rand_latents = randn_tensor(self.shape, device=self.device, dtype=self.dtype)
            dis = self.eval_watermark(rand_latents)
            dis_all.append(dis)
        dis_all = np.array(dis_all)
        return dis_all.mean(), dis_all.var()

    # the probability of being watermarked
    def one_minus_p_value(self, latents):
        l1_metric = self.eval_watermark(latents)
        return abs(0.5 - norm.cdf(l1_metric, self.mu, self.sigma))*2
    
    # def tree_ring_p_value(self, latents):
    #     target_patch = self.gt_patch[self.watermarking_mask].flatten()
    #     target_patch = torch.concatenate([target_patch.real, target_patch.imag])

    #     reversed_latents_w_fft = torch.fft.fftshift(torch.fft.fft2(latents), dim=(-1, -2))[self.watermarking_mask].flatten()
    #     reversed_latents_w_fft = torch.concatenate([reversed_latents_w_fft.real, reversed_latents_w_fft.imag])
        
    #     sigma_w = reversed_latents_w_fft.std()
    #     lambda_w = (target_patch ** 2 / sigma_w ** 2).sum().item()
    #     x_w = (((reversed_latents_w_fft - target_patch) / sigma_w) ** 2).sum().item()
    #     p_w = scipy.stats.ncx2.cdf(x=x_w, df=len(target_patch), nc=lambda_w)
    #     return p_w

    def tree_ring_p_value(self, latents):
        # Resize mask if needed
        if self.watermarking_mask.shape[-2:] != latents.shape[-2:]:
            mask_resized = torch.nn.functional.interpolate(
                self.watermarking_mask.float(), size = latents.shape[-2:], mode = 'bilinear'
            ).bool()
        else:
            mask_resized = self.watermarking_mask
    
        # Resize patch if needed
        if self.gt_patch.shape[-2:] != latents.shape[-2:]:
            patch_resized = torch.nn.functional.interpolate(
                self.gt_patch.real, size = latents.shape[-2:], mode = 'bilinear'
            )
        else:
            patch_resized = self.gt_patch.real

        # Compute FFT of latents
        latents_fft = torch.fft.fftshift(torch.fft.fft2(latents), dim=(-1, -2))
    
        # Index FFT and patch using the resized mask
        target_patch = patch_resized[mask_resized].flatten()
        latents_fft_masked = latents_fft[mask_resized].flatten()
    
        # Concatenate real and imaginary parts
        # target_patch_concat = torch.cat([target_patch.real, target_patch.imag])
        target_patch_concat = target_patch
        latents_fft_concat = torch.cat([latents_fft_masked.real, latents_fft_masked.imag])
    
        # Compute chi-square statistics
        sigma_w = latents_fft_concat.std()
        lambda_w = (target_patch_concat ** 2 / sigma_w ** 2).sum().item()
        x_w = (((latents_fft_concat - target_patch_concat) / sigma_w) ** 2).sum().item()
        p_w = scipy.stats.ncx2.cdf(x=x_w, df=len(target_patch_concat), nc=lambda_w)
    
        return p_w

        

class GTWatermarkMulti(GTWatermark):
    def __init__(self, device, shape=(1,4,64,64), dtype=torch.float32, w_settings={0:[1,5,9], 1:[2,6,10], 2:[3,7], 3:[4,8]}, generator=None):
        self.device = device
        # from latent tensor
        self.shape = shape
        self.dtype = dtype
        # from hyperparameters
        self.w_settings = w_settings

        self.gt_patch, self.watermarking_mask = self._gen_gt(generator=generator)
        self.mu, self.sigma = self.watermark_stat()

    def _get_watermarking_pattern(self, gt_init): # in fft space
        gt_patch = torch.fft.fftshift(torch.fft.fft2(gt_init), dim=(-1, -2))
        watermarking_mask = torch.zeros(gt_init.shape, dtype=torch.bool).to(self.device)
        for w_channel in self.w_settings:
            for w_radius in self.w_settings[w_channel]:
                tmp_mask_alter, tmp_mask_inner = self._circle_mask(gt_init.shape[-1], r=w_radius), self._circle_mask(gt_init.shape[-1], r=w_radius-1) 
                tmp_mask = torch.tensor(np.logical_xor(tmp_mask_alter,tmp_mask_inner)).to(self.device) 
                gt_patch[:, w_channel, tmp_mask] = gt_patch[0, w_channel, 0, w_radius].item()
                watermarking_mask[:, w_channel, tmp_mask] = True
        return gt_patch, watermarking_mask

    def _gen_gt(self, generator=None):
        gt_init = randn_tensor(self.shape, generator=generator, device=self.device, dtype=self.dtype)
        gt_patch, watermarking_mask = self._get_watermarking_pattern(gt_init)
        return gt_patch, watermarking_mask

    # def eval_watermark(self, latents_w):
    #     latents_w_fft = torch.fft.fftshift(torch.fft.fft2(latents_w), dim=(-1, -2))
    #     l1_tensor = torch.abs(latents_w_fft[self.watermarking_mask] - self.gt_patch[self.watermarking_mask])
        
    #     # num_samples = len(l1_tensor) // 2 
    #     num_samples = 400
    #     indices = torch.randint(0, len(l1_tensor), (num_samples,), generator=torch.Generator().manual_seed(0))
    #     sampled_elements = l1_tensor[indices]
    #     l1_metric = sampled_elements.mean().item()
    #     return l1_metric
