import os
import json
from typing import Optional, Tuple, Union
from collections import OrderedDict

import torch
from torch import nn
from torch import distributed as dist
from transformers.activations import NewGELUActivation
from transformers.modeling_outputs import CausalLMOutputWithPast
from deepspeed.pipe import LayerSpec

from collie.log import logger
from collie.module import (ColumnParallelLinearWithoutBias,
                           RowParallelLinearWithoutBias,
                           VocabParallelEmbedding,
                           ColumnParallelLMHead)
from collie.driver.io.file import FileIODriver
from collie.driver.io.petrel import PetrelIODriver
from collie.models.base import CollieModelForCausalLM
from collie.utils import env, progress
from collie.config import CollieConfig
from .utils import (apply_rotary_pos_emb, create_sinusoidal_positions,
                    set_index_dict, _state_dict_to_save, _state_dict_to_load,
                    _weight_name_in_current_rank)

__all__ = ["MossForCausalLM"]

class MossAttention(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.config = config
        max_positions = config.n_positions
        self.register_buffer(
            "causal_mask",
            torch.tril(torch.ones((max_positions, max_positions), dtype=torch.bool)).view(
                1, 1, max_positions, max_positions
            ),
        )

        self.attn_dropout = nn.Dropout(config.attn_pdrop)
        self.resid_dropout = nn.Dropout(config.resid_pdrop)

        self.embed_dim = config.n_embd
        self.num_attention_heads = config.n_head
        self.head_dim = self.embed_dim // self.num_attention_heads
        if self.head_dim * self.num_attention_heads != self.embed_dim:
            raise ValueError(
                f"embed_dim must be divisible by num_attention_heads (got `embed_dim`: {self.embed_dim} and"
                f" `num_attention_heads`: {self.num_attention_heads})."
            )
        self.scale_attn = torch.sqrt(torch.tensor(self.head_dim, dtype=torch.float32)).to(torch.get_default_dtype())
        self.qkv_proj = ColumnParallelLinearWithoutBias(
            self.embed_dim, self.embed_dim * 3, bias=False, gather_output=True,
            use_cpu_initialization=config.use_cpu_initialization
        )

        self.out_proj = RowParallelLinearWithoutBias(
            self.embed_dim, self.embed_dim, bias=False,
            use_cpu_initialization=config.use_cpu_initialization,
            input_is_parallel=False
        )
        self.rotary_dim = config.rotary_dim
        pos_embd_dim = self.rotary_dim or self.embed_dim
        self.embed_positions = create_sinusoidal_positions(max_positions, pos_embd_dim)

    def _split_heads(self, x, n_head, dim_head, mp_num):
        reshaped = x.reshape(x.shape[:-1] + (n_head // mp_num, dim_head))
        reshaped = reshaped.reshape(x.shape[:-2] + (-1,) + reshaped.shape[-1:])
        return reshaped

    def _merge_heads(self, tensor, num_attention_heads, attn_head_size):
        """
        Merges attn_head_size dim and num_attn_heads dim into n_ctx
        """
        if len(tensor.shape) == 5:
            tensor = tensor.permute(0, 1, 3, 2, 4).contiguous()
        elif len(tensor.shape) == 4:
            tensor = tensor.permute(0, 2, 1, 3).contiguous()
        else:
            raise ValueError(f"Input tensor rank should be one of [4, 5], but is: {len(tensor.shape)}")
        new_shape = tensor.size()[:-2] + (num_attention_heads * attn_head_size,)
        return tensor.view(new_shape)

    def _attn(
        self,
        query,
        key,
        value,
        attention_mask=None,
        head_mask=None,
    ):
        # compute causal mask from causal mask buffer
        query_length, key_length = query.size(-2), key.size(-2)
        causal_mask = self.causal_mask[:, :, key_length - query_length : key_length, :key_length]

        # Keep the attention weights computation in fp32 to avoid overflow issues
        query = query.to(torch.float32)
        key = key.to(torch.float32)

        attn_weights = torch.matmul(query, key.transpose(-1, -2))

        attn_weights = attn_weights / self.scale_attn
        mask_value = torch.finfo(attn_weights.dtype).min
        # Need to be a tensor, otherwise we get error: `RuntimeError: expected scalar type float but found double`.
        # Need to be on the same device, otherwise `RuntimeError: ..., x and y to be on the same device`
        mask_value = torch.tensor(mask_value, dtype=attn_weights.dtype).to(attn_weights.device)
        attn_weights = torch.where(causal_mask, attn_weights, mask_value)

        if attention_mask is not None:
            # Apply the attention mask
            attn_weights = attn_weights + attention_mask

        attn_weights = nn.Softmax(dim=-1)(attn_weights)
        attn_weights = attn_weights.to(value.dtype)
        attn_weights = self.attn_dropout(attn_weights)

        # Mask heads if we want to
        if head_mask is not None:
            attn_weights = attn_weights * head_mask

        attn_output = torch.matmul(attn_weights, value)

        return attn_output, attn_weights

    def forward(
        self,
        hidden_states: Optional[torch.FloatTensor],
        layer_past: Optional[Tuple[torch.Tensor]] = None,
        attention_mask: Optional[torch.FloatTensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = False,
    ) -> Union[
        Tuple[torch.Tensor, Tuple[torch.Tensor]],
        Optional[Tuple[torch.Tensor, Tuple[torch.Tensor], Tuple[torch.Tensor, ...]]],
    ]:
        qkv = self.qkv_proj(hidden_states)
        # TODO(enijkamp): factor out number of logical TPU-v4 cores or make forward pass agnostic
        mp_num = 4
        qkv_split = qkv.reshape(qkv.shape[:-1] + (mp_num, -1))

        local_dim = self.head_dim * self.num_attention_heads // mp_num
        query, value, key = torch.split(qkv_split, local_dim, dim=-1)
        query = self._split_heads(query, self.num_attention_heads, self.head_dim, mp_num=mp_num)
        key = self._split_heads(key, self.num_attention_heads, self.head_dim, mp_num=mp_num)

        value = self._split_heads(value, self.num_attention_heads, self.head_dim, mp_num=mp_num)

        embed_positions = self.embed_positions
        if embed_positions.device != position_ids.device:
            embed_positions = embed_positions.to(position_ids.device)
            self.embed_positions = embed_positions

        sincos = embed_positions[position_ids]
        sin, cos = torch.split(sincos, sincos.shape[-1] // 2, dim=-1)

        if self.rotary_dim is not None:
            k_rot = key[:, :, :, : self.rotary_dim]
            k_pass = key[:, :, :, self.rotary_dim :]

            q_rot = query[:, :, :, : self.rotary_dim]
            q_pass = query[:, :, :, self.rotary_dim :]

            k_rot = apply_rotary_pos_emb(k_rot, sin, cos)
            q_rot = apply_rotary_pos_emb(q_rot, sin, cos)

            key = torch.cat([k_rot, k_pass], dim=-1)
            query = torch.cat([q_rot, q_pass], dim=-1)
        else:
            key = apply_rotary_pos_emb(key, sin, cos)
            query = apply_rotary_pos_emb(query, sin, cos)

        if layer_past is not None:
            past_key = layer_past[0]
            past_value = layer_past[1]
            key = torch.cat((past_key, key), dim=1)
            value = torch.cat((past_value, value), dim=1)

        if use_cache is True:
            present = (key, value)
        else:
            present = None

        key = key.permute(0, 2, 1, 3)
        query = query.permute(0, 2, 1, 3)
        value = value.permute(0, 2, 1, 3)

        # compute self-attention: V x Softmax(QK^T)
        attn_output, attn_weights = self._attn(query, key, value, attention_mask, head_mask)

        attn_output = self._merge_heads(attn_output, self.num_attention_heads, self.head_dim)
        attn_output = self.out_proj(attn_output)
        attn_output = self.resid_dropout(attn_output)

        outputs = (attn_output, present)

        return outputs  # a, present


class MossMLP(nn.Module):
    def __init__(self, intermediate_size, config):  # in MLP: intermediate_size= 4 * embed_dim
        super().__init__()
        embed_dim = config.n_embd

        self.fc_in = ColumnParallelLinearWithoutBias(
            embed_dim, intermediate_size, gather_output=False,
            use_cpu_initialization=config.use_cpu_initialization
        )
        self.fc_out = RowParallelLinearWithoutBias(
            intermediate_size, embed_dim, input_is_parallel=True,
            use_cpu_initialization=config.use_cpu_initialization
        )

        self.act = NewGELUActivation()
        self.dropout = nn.Dropout(config.resid_pdrop)

    def forward(self, hidden_states: Optional[torch.FloatTensor]) -> torch.FloatTensor:
        hidden_states = self.fc_in(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.fc_out(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states


class MossBlock(nn.Module):
    def __init__(self, config, layer_idx):
        super().__init__()
        inner_dim = config.n_inner if config.n_inner is not None else 4 * config.n_embd
        self.ln_1 = nn.LayerNorm(config.n_embd, eps=config.layer_norm_epsilon)
        self.attn = MossAttention(config)
        self.mlp = MossMLP(inner_dim, config)
        self.config = config
        self.idx = layer_idx

        self.use_cache = True
        self.past_key_values = None
        self.hidden_states = None

    def _forward(
        self,
        hidden_states: Optional[torch.FloatTensor],
        layer_past: Optional[Tuple[torch.Tensor]] = None,
        attention_mask: Optional[torch.FloatTensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = False,
    ) -> Union[Tuple[torch.Tensor], Optional[Tuple[torch.Tensor, Tuple[torch.FloatTensor, ...]]]]:
        residual = hidden_states
        hidden_states = self.ln_1(hidden_states)
        attn_outputs = self.attn(
            hidden_states=hidden_states,
            layer_past=layer_past,
            attention_mask=attention_mask,
            position_ids=position_ids,
            head_mask=head_mask,
            use_cache=use_cache,
        )
        attn_output = attn_outputs[0]  # output_attn: a, present, (attentions)
        outputs = attn_outputs[1:]

        feed_forward_hidden_states = self.mlp(hidden_states)
        hidden_states = attn_output + feed_forward_hidden_states + residual

        if use_cache:
            outputs = (hidden_states,) + outputs
        else:
            outputs = (hidden_states,) + outputs[1:]

        return outputs  # hidden_states, present, (attentions)

    def forward(self, hidden_states):
        if not self.training:
            self.hidden_states = hidden_states
        use_cache = not self.training and self.use_cache
        end_pos = hidden_states.shape[1]
        if end_pos != 1:
            self.past_key_values = None
        if not use_cache and self.past_key_values is not None:
            self.past_key_values = None

        if self.past_key_values is None or self.training:
            past_length = 0
        else:
            past_length = self.past_key_values[0].size(1)

        position_ids = torch.arange(
            past_length, end_pos + past_length, dtype=torch.long).cuda()
        position_ids = position_ids.unsqueeze(0).view(-1, end_pos)

        if self.config.gradient_checkpointing and self.training:

            def create_custom_forward(module):
                def custom_forward(*inputs):
                    # None for past_key_value
                    return module(*inputs)

                return custom_forward

            outputs = torch.utils.checkpoint.checkpoint(
                create_custom_forward(self._forward),
                hidden_states,
                None,
                None,
                position_ids,
            )
        else:
            outputs = self._forward(
                hidden_states,
                position_ids=position_ids,
                layer_past=self.past_key_values,
                attention_mask=None,
                head_mask=None,
                use_cache=use_cache
            )

        if use_cache:
            self.past_key_values = outputs[1]

        # hidden_states 
        return outputs[0]


class MossForCausalLM(CollieModelForCausalLM):
    """
    支持 3D 并行的 Moss 模型。

    :param config: :class:`.CollieConfig`
    """
    def __init__(self, config):
        super().__init__(config)
        self.embed_dim = config.n_embd
        self.vocab_size = config.vocab_size
        self.wte = VocabParallelEmbedding(config.vocab_size, self.embed_dim,
                                          use_cpu_initialization=config.use_cpu_initialization)
        self.drop = nn.Dropout(config.embd_pdrop)
        self.h = nn.ModuleList([
            MossBlock(config, i) for i in range(config.n_layer)
        ])
        self.ln_f = nn.LayerNorm(self.embed_dim, eps=config.layer_norm_epsilon)
        self.lm_head = ColumnParallelLMHead(
            config.n_embd, config.vocab_size, use_cpu_initialization=config.use_cpu_initialization
        )

    def forward(self, input_ids, **kwargs):
        inputs_embed = self.wte(input_ids)
        hidden_states = self.drop(inputs_embed)

        all_hidden_states = ()
        for l in self.h:
            all_hidden_states += (hidden_states,)
            hidden_states = l(hidden_states)

        hidden_states = self.ln_f(hidden_states)
        logits = self.lm_head(hidden_states)
        return CausalLMOutputWithPast(
            loss=None,
            logits=logits,
            past_key_values=self._get_past_key_values(self.h),
            hidden_states=all_hidden_states,
            attentions=None
        )
    
    def prepare_inputs_for_generation(self, 
                                      input_ids: torch.Tensor,
                                      past_key_values: Optional[list] = None,
                                      attention_mask: Optional[torch.Tensor] = None,
                                      **kwargs):
        self._set_use_cache(self.h, self.generation_config.use_cache)
        if past_key_values is None:
            self._clean_past_key_values(self.h)
        else:
            input_ids = input_ids[:, -1].unsqueeze(-1)
            self._set_past_key_values(self.h, past_key_values)
        return {"input_ids": input_ids}
    
    def clean(self):
        self._clean_hidden_states([*self.h, self.lm_head])
        self._clean_past_key_values(self.h)
    
    @classmethod
    def pipeline_layers(cls, config):
        if isinstance(config, str):
            config = CollieConfig.from_pretrained(config)
        layers = [
            VocabParallelEmbedding(
                config.vocab_size, config.n_embd,
                use_cpu_initialization=config.use_cpu_initialization
            ),
            nn.Dropout(config.embd_pdrop),
        ]
        layers += [
            LayerSpec(MossBlock, config, i) for i in range(config.n_layer)
        ]
        layers += [
            nn.LayerNorm(config.n_embd, eps=config.layer_norm_epsilon),
            ColumnParallelLMHead(
                config.n_embd, config.vocab_size,
                use_cpu_initialization=config.use_cpu_initialization
            )
        ]

        return layers
    
    @staticmethod
    def load_parallel_state_dict(path: str, config: Union[CollieConfig, str],
                                 process_exclusion: bool = False, **kwargs):...
    @staticmethod
    def load_parallel_state_dict(path: str,
                                 config: Union[CollieConfig, str],
                                 process_exclusion: bool = False,
                                 protocol: str = 'file',
                                 format: str = 'hf', **kwargs):
        """
        Load state_dict from ``path``.

        :return: state_dict. Note that the state_dict should be processed
            properly to match the current rank.
        """
        assert format in ["hf", "meta"], f"Only support hf and meta , not `{format}`."
        assert protocol in ["file", "petrel"], f"Only support file and petrel protocol, not `{protocol}`."
        # Actually Moss only supports `hf` format
        if isinstance(config, str):
            config = CollieConfig.from_pretrained(config)
        IODriver = FileIODriver if protocol == 'file' else PetrelIODriver
        if not IODriver.exists(path) and protocol == "file":
            raise FileNotFoundError(f"folder {path} not found.")

        # 如果开启了进程互斥，那么每个进程都会显示进度条，否则只显示 RANK0 的
        hide_progress = not process_exclusion and env.rank != 0
        for cur_rank in range(dist.get_world_size()):
            if process_exclusion:
                dist.barrier()
            if cur_rank != env.rank:
                continue
            if IODriver.exists(os.path.join(path, "config.json")):
                # update config from config.json
                new_config = json.loads(IODriver.load(os.path.join(path, "config.json"), mode="r"))
                config.model_config.update(new_config)
            # 如果存在 pytorch_model.bin.index.json 文件的话，此时不同的 pp 进程可以按需加载自己需要的权重
            index_file = os.path.join(path, "pytorch_model.bin.index.json")
            # start load
            state_dict = OrderedDict()
            if IODriver.exists(index_file) and env.is_pipeline:
                # 有 index 且是流水线
                weight_map = json.loads(IODriver.load(index_file, mode="r"))["weight_map"]
                # layers 表示当前 rank 自己需要的层
                cur_names = _weight_name_in_current_rank(weight_map.keys())
                weights = set(weight_map[name] for name in cur_names)
            else:
                # 如果没有 pytorch_model.bin.index.json 文件的话，那么就加载所有的权重
                weights = [weight for weight in IODriver.list(path) if weight.endswith(".bin")]

            desc = "Loading state dict"
            if process_exclusion:
                desc += f" on pp={env.pp_rank} tp={env.tp_rank} dp={env.dp_rank}"
            for weight in progress(weights, desc, disable=hide_progress):
                part_state_dict = IODriver.load(os.path.join(path, weight), mode="rb")
                state_dict.update(_state_dict_to_load(
                    part_state_dict, env.tp_rank, config.tp_size,
                    process_exclusion
                ))

        return state_dict

    @staticmethod
    def save_parallel_state_dict(state_dict: dict, path: str,
                                 config: CollieConfig,
                                 process_exclusion: bool = False, **kwargs):...
    @staticmethod
    def save_parallel_state_dict(state_dict: dict,
                                 path: str, 
                                 config: CollieConfig,
                                 process_exclusion: bool = False,
                                 protocol: str = 'file'):
        """
        Save state_dict to ``path``.
        """
        assert protocol in ["file", "petrel"], f"Only support file and petrel protocol, not `{protocol}`."
        IODriver = FileIODriver if protocol == 'file' else PetrelIODriver
        if env.rank == 0:
            config.save_pretrained(path)

        # gather to tp rank 0
        desc = "Saving state dict"
        # 没有 process_exclusion 的时候就不显示了
        hide_progress = process_exclusion and env.rank != 0
        for cur_pp_rank in progress(range(env.pp_size), desc, disable=hide_progress):
            if process_exclusion:
                dist.barrier()
            if env.dp_rank != 0:
                continue
            # continue execution when dp_rank == 0
            if cur_pp_rank != env.pp_rank:
                continue
            # continue when pp_rank is available

            state_dict = _state_dict_to_save(
                state_dict, env.tp_rank, config.tp_size, env.tp_group,
                process_exclusion
            )
            if env.tp_rank != 0:
                continue
            # save at tp_rank 0
            # Save gathered weights
            if env.is_pipeline:
                ckpt_name = f"pytorch_model-{env.pp_rank+1:05d}-of-{config.pp_size:05d}.bin"
                index_dict = set_index_dict(state_dict, ckpt_name)
                tmp_index_file = os.path.join(path, "_tmp_index_{}.json")
                IODriver.save(
                    json.dumps(index_dict), tmp_index_file.format(env.pp_rank)
                )
            else:
                ckpt_name = f"pytorch_model.bin"
            ckpt_path = os.path.join(path, ckpt_name)
            IODriver.save(state_dict, ckpt_path)
        dist.barrier()

        # Only save and merge on rank0
        if env.rank == 0 and env.is_pipeline:
            # merge
            tmp_index_files = [tmp_index_file.format(i) for i in range(config.pp_size)]
            total_size = 0
            weight_map = {}
            for _file in tmp_index_files:
                _index_dict = json.loads(IODriver.load(_file, mode="r"))
                total_size += _index_dict["total_size"]
                weight_map.update(_index_dict["weight_map"])
                os.remove(_file)
            merged_dict = {
                "metadata": {"total_size": total_size},
                "weight_map": weight_map
            }
            IODriver.save(
                json.dumps(merged_dict, indent=2, sort_keys=True) + "\n",
                os.path.join(path, "pytorch_model.bin.index.json")
            )
        dist.barrier()
