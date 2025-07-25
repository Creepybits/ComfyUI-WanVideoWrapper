# based on https://github.com/mit-han-lab/radial-attention/blob/main/radial_attn/attn_mask.py
import torch
try:
    from sparse_sageattn import sparse_sageattn
except:
    try:
        from .sparse_sage.core import sparse_sageattn
    except:
        sparse_sageattn = None
        raise ImportError("sparse_sageattn is not available. Please install the sparse_sageattn package or check your import path.")

from comfy import model_management as mm
device = mm.get_torch_device()

from tqdm import tqdm

def shrinkMaskStrict(mask, block_size=128):
    seqlen = mask.shape[0]
    block_num = seqlen // block_size
    mask = mask[:block_num * block_size, :block_num * block_size].view(block_num, block_size, block_num, block_size)
    col_densities = mask.sum(dim = 1) / block_size
    # we want the minimum non-zero column density in the block
    non_zero_densities = col_densities > 0
    high_density_cols = col_densities > 1/3
    frac_high_density_cols = high_density_cols.sum(dim=-1) / (non_zero_densities.sum(dim=-1) + 1e-9)
    block_mask = frac_high_density_cols > 0.6
    block_mask[0:0] = True
    block_mask[-1:-1] = True
    return block_mask

def get_diagonal_split_mask(i, j, token_per_frame, sparse_type):
    assert(sparse_type in ["radial"])
    dist = abs(i - j)
    group = dist.bit_length()
    threshold = 128 # hardcoded threshold for now, which is equal to block-size
    decay_length = 2 ** token_per_frame.bit_length() / 2 ** group
    if decay_length >= threshold:
        return torch.ones((token_per_frame, token_per_frame), device=device, dtype=torch.bool)
    
    split_factor = int(threshold / decay_length)
    modular = dist % split_factor
    if modular == 0:
        return torch.ones((token_per_frame, token_per_frame), device=device, dtype=torch.bool)
    else:
        return torch.zeros((token_per_frame, token_per_frame), device=device, dtype=torch.bool)

def get_window_width(i, j, token_per_frame, sparse_type, decay_factor=1, block_size=128):
    assert(sparse_type in ["radial"])
    dist = abs(i - j)
    if dist < 1:
        return token_per_frame
    if dist == 1:
        return token_per_frame // 2
    group = dist.bit_length()
    decay_length = 2 ** token_per_frame.bit_length() / 2 ** group * decay_factor
    threshold = block_size
    if decay_length >= threshold:
        return decay_length
    else:
        return threshold

def gen_log_mask_shrinked(device, s, video_token_num, num_frame, block_size=128, sparse_type="log", decay_factor=0.5):
    """
    A more memory friendly version, we generate the attention mask of each frame pair at a time,
    shrinks it, and stores it into the final result
    """

    final_log_mask = torch.zeros((s // block_size, s // block_size), device=device, dtype=torch.bool)
    token_per_frame = video_token_num // num_frame
    video_text_border = video_token_num // block_size

    col_indices = torch.arange(0, token_per_frame, device=device).view(1, -1)
    row_indices = torch.arange(0, token_per_frame, device=device).view(-1, 1)
    final_log_mask[video_text_border:] = True
    final_log_mask[:, video_text_border:] = True
    for i in tqdm(range(num_frame), desc="Frames (i)"):
        for j in range(num_frame):
            local_mask = torch.zeros((token_per_frame, token_per_frame), device=device, dtype=torch.bool)
            if j == 0: # this is attention sink
                local_mask = torch.ones((token_per_frame, token_per_frame), device=device, dtype=torch.bool)
            else:
                window_width = get_window_width(i, j, token_per_frame, sparse_type, decay_factor=decay_factor, block_size=block_size)
                local_mask = torch.abs(col_indices - row_indices) <= window_width
                split_mask = get_diagonal_split_mask(i, j, token_per_frame, sparse_type)
                local_mask = torch.logical_and(local_mask, split_mask)

            remainder_row = (i * token_per_frame) % block_size
            remainder_col = (j * token_per_frame) % block_size
            # get the padded size
            all_length_row = remainder_row + ((token_per_frame - 1) // block_size + 1) * block_size
            all_length_col = remainder_col + ((token_per_frame - 1) // block_size + 1) * block_size
            padded_local_mask = torch.zeros((all_length_row, all_length_col), device=device, dtype=torch.bool)
            padded_local_mask[remainder_row:remainder_row + token_per_frame, remainder_col:remainder_col + token_per_frame] = local_mask
            # shrink the mask
            block_mask = shrinkMaskStrict(padded_local_mask, block_size=block_size)
            # set the block mask to the final log mask
            block_row_start = (i * token_per_frame) // block_size
            block_col_start = (j * token_per_frame) // block_size
            block_row_end = block_row_start + block_mask.shape[0]
            block_col_end = block_col_start + block_mask.shape[1]
            final_log_mask[block_row_start:block_row_end, block_col_start:block_col_end] = torch.logical_or(
                final_log_mask[block_row_start:block_row_end, block_col_start:block_col_end], block_mask)
    #print(f"mask sparsity: {1 - final_log_mask.sum() / final_log_mask.numel()}")
    return final_log_mask

class MaskMap:

    def __init__(self, video_token_num=25440, num_frame=16):
        self.video_token_num = video_token_num
        self.num_frame = num_frame
        self.log_mask = None

    def queryLogMask(self, seq_len, sparse_type, block_size=128, decay_factor=0.5):
        log_mask = torch.ones((seq_len // block_size, seq_len // block_size), device=device, dtype=torch.bool)
        if self.log_mask is None:
            self.log_mask = gen_log_mask_shrinked(device, seq_len, self.video_token_num, self.num_frame, sparse_type=sparse_type, decay_factor=decay_factor, block_size=block_size)
        block_bound = self.video_token_num // block_size
        log_mask[:block_bound, :block_bound] = self.log_mask[:block_bound, :block_bound]
        return log_mask

@torch.compiler.disable()
def RadialSpargeSageAttnDense(query, key, value, mask_map=None):
    # dense case
    output_video = sparse_sageattn(
        query[:,:mask_map.video_token_num],
        key[:,:key.shape[1], :, :],
        value[:,:key.shape[1], :, :],
        mask_id=None,
        is_causal=False,
        tensor_layout="NHD",
    )

    return output_video.contiguous()

@torch.compiler.disable()
def RadialSpargeSageAttn(query, key, value, mask_map, block_size=128, decay_factor=1):
    # Simple cache based on function arguments
    if not hasattr(RadialSpargeSageAttn, "_cache"):
        RadialSpargeSageAttn._cache = {}
    cache_key = (query.shape[-2], block_size, decay_factor)
    if cache_key in RadialSpargeSageAttn._cache:
        input_mask = RadialSpargeSageAttn._cache[cache_key]
    else:
        print("Radial Attention: Generating block mask")
        video_mask = mask_map.queryLogMask(query.shape[0] * query.shape[1], "radial", block_size=block_size, decay_factor=decay_factor) 
        mask = torch.repeat_interleave(video_mask, 2, dim=1) #s, t
        input_mask = mask.unsqueeze(0).unsqueeze(1).expand(1, query.shape[-2], mask.shape[0], mask.shape[1]) # b, h, s, t
        RadialSpargeSageAttn._cache[cache_key] = input_mask

    output = sparse_sageattn(
        query[:, :, :mask_map.video_token_num, :],
        key[:, :, :mask_map.video_token_num, :],
        value[:, :, :mask_map.video_token_num, :],
        mask_id=input_mask.to(torch.int8),
        is_causal=False,
        tensor_layout="NHD"
    )
    
    return output.contiguous()