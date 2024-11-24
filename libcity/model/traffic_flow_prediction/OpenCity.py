import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from functools import partial
import torch
import torch.nn as nn
from logging import getLogger
from libcity.model.abstract_traffic_state_model import AbstractTrafficStateModel


def drop_path(x, drop_prob=0., training=False):
    if drop_prob == 0. or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (x.ndim - 1)
    random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    random_tensor.floor_()
    output = x.div(keep_prob) * random_tensor
    return output


class DropPath(nn.Module):
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)


class LlamaRMSNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6):
        """
        LlamaRMSNorm is equivalent to T5LayerNorm
        """
        super().__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.variance_epsilon = eps

    def forward(self, hidden_states):
        variance = hidden_states.pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states


class PositionalEncoding(nn.Module):
    def __init__(self, embed_dim, max_len=100):
        super(PositionalEncoding, self).__init__()
        pe = torch.zeros(max_len, embed_dim).float()
        pe.require_grad = False

        position = torch.arange(0, max_len).float().unsqueeze(1)
        div_term = (torch.arange(0, embed_dim, 2).float() * -(math.log(10000.0) / embed_dim)).exp()

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)

        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)

    def forward(self, x):
        return self.pe[:, :x.size(2)].unsqueeze(1).expand_as(x)


class PatchEmbedding_flow(nn.Module):
    def __init__(self, d_model, patch_len, stride, padding, his):
        super(PatchEmbedding_flow, self).__init__()
        # Patching
        self.patch_len = patch_len
        self.stride = stride
        self.his = his

        self.value_embedding = nn.Linear(patch_len, d_model, bias=False)
        self.position_encoding = PositionalEncoding(d_model)

    def forward(self, x):
        # do patching
        x = x.squeeze(-1).permute(0, 2, 1)
        if self.his == x.shape[-1]:
            x = x.unfold(dimension=-1, size=self.patch_len, step=self.stride)
        else:
            gap = self.his // x.shape[-1]
            x = x.unfold(dimension=-1, size=self.patch_len // gap, step=self.stride // gap)
            x = F.pad(x, (0, (self.patch_len - self.patch_len // gap)))
        x = self.value_embedding(x)
        x = x + self.position_encoding(x)
        x = x.permute(0, 2, 1, 3)
        return x


class PatchEmbedding_time(nn.Module):
    def __init__(self, d_model, patch_len, stride, padding, his):
        super(PatchEmbedding_time, self).__init__()
        # Patching
        self.patch_len = patch_len
        self.stride = stride
        self.his = his
        self.minute_size = 1440 + 1
        self.daytime_embedding = nn.Embedding(self.minute_size, d_model // 2)
        weekday_size = 7 + 1
        self.weekday_embedding = nn.Embedding(weekday_size, d_model // 2)

    def forward(self, x):
        # do patching
        bs, ts, nn, dim = x.size()
        x = x.permute(0, 2, 3, 1).reshape(bs, -1, ts)
        if self.his == x.shape[-1]:
            x = x.unfold(dimension=-1, size=self.patch_len, step=self.stride)
        else:
            gap = self.his // x.shape[-1]
            x = x.unfold(dimension=-1, size=self.patch_len // gap, step=self.stride // gap)
        num_patch = x.shape[-2]
        x = x.reshape(bs, nn, dim, num_patch, -1).transpose(1, 3)
        x_tdh = x[:, :, 0, :, 0]
        x_dwh = x[:, :, 1, :, 0]
        x_tdp = x[:, :, 2, :, 0]
        x_dwp = x[:, :, 3, :, 0]

        x_tdh = self.daytime_embedding(x_tdh)
        x_dwh = self.weekday_embedding(x_dwh)
        x_tdp = self.daytime_embedding(x_tdp)
        x_dwp = self.weekday_embedding(x_dwp)
        x_th = torch.cat([x_tdh, x_dwh], dim=-1)
        x_tp = torch.cat([x_tdp, x_dwp], dim=-1)

        return x_th, x_tp


class LaplacianPE(nn.Module):
    def __init__(self, lape_dim, embed_dim):
        super().__init__()
        self.embedding_lap_pos_enc = nn.Linear(lape_dim, embed_dim)

    def forward(self, lap_mx):
        lap_pos_enc = self.embedding_lap_pos_enc(lap_mx).unsqueeze(0).unsqueeze(0)
        return lap_pos_enc


class GCN(nn.Module):
    def __init__(self, in_dim, out_dim, prob_drop, alpha):
        super(GCN, self).__init__()
        self.fc1 = nn.Linear(in_dim, out_dim, bias=False)
        self.mlp = nn.Linear(out_dim, out_dim)
        self.dropout = prob_drop
        self.alpha = alpha

    def forward(self, x, adj):
        d = adj.sum(1)
        h = x
        a = adj / d.view(-1, 1)
        gcn_out = self.fc1(torch.einsum('bdkt,nk->bdnt', h, a))
        out = self.alpha * x + (1 - self.alpha) * gcn_out
        ho = self.mlp(out)
        return ho


class FeedForward(nn.Module):
    def __init__(self, hidden_size: int, intermediate_size: int) -> None:
        super().__init__()

        self.w1 = nn.Linear(hidden_size, intermediate_size, bias=False)
        self.w2 = nn.Linear(intermediate_size, hidden_size, bias=False)
        self.w3 = nn.Linear(hidden_size, intermediate_size, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.w2(F.silu(self.w1(x)) * self.w3(x))


class TemporalSelfAttention(nn.Module):
    def __init__(
            self, dim, t_attn_size, t_num_heads=6, tc_num_heads=6, qkv_bias=False,
            attn_drop=0., proj_drop=0., device=torch.device('cpu'),
    ):
        super().__init__()
        assert dim % t_num_heads == 0
        self.t_num_heads = t_num_heads
        self.tc_num_heads = tc_num_heads
        self.head_dim = dim // t_num_heads
        self.scale = self.head_dim ** -0.5
        self.device = device
        self.t_attn_size = t_attn_size

        self.t_q_conv = nn.Linear(dim, dim, bias=qkv_bias)
        self.t_k_conv = nn.Linear(dim, dim, bias=qkv_bias)
        self.t_v_conv = nn.Linear(dim, dim, bias=qkv_bias)
        self.t_attn_drop = nn.Dropout(attn_drop)

        self.norm_tatt1 = LlamaRMSNorm(dim)
        self.norm_tatt2 = LlamaRMSNorm(dim)

        self.tc_q_conv = nn.Linear(dim, dim, bias=qkv_bias)
        self.tc_k_conv = nn.Linear(dim, dim, bias=qkv_bias)
        self.tc_v_conv = nn.Linear(dim, dim, bias=qkv_bias)
        self.tc_attn_drop = nn.Dropout(attn_drop)

        self.GCN = GCN(dim, dim, proj_drop, alpha=0.05)
        self.act = nn.GELU()

        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x_q, x_k, x_v, TH, TP, adj, geo_mask=None, sem_mask=None, trg_mask=False):
        B, T_q, N, D = x_q.shape
        T_k, T_v = x_k.shape[1], x_v.shape[1]

        tc_q = self.tc_q_conv(TP).transpose(1, 2)
        tc_k = self.tc_k_conv(TH).transpose(1, 2)
        tc_v = self.tc_v_conv(x_q).transpose(1, 2)
        tc_q = tc_q.reshape(B, N, T_q, self.tc_num_heads, self.head_dim).permute(0, 1, 3, 2, 4)
        tc_k = tc_k.reshape(B, N, T_k, self.tc_num_heads, self.head_dim).permute(0, 1, 3, 2, 4)
        tc_v = tc_v.reshape(B, N, T_v, self.tc_num_heads, self.head_dim).permute(0, 1, 3, 2, 4)
        tc_attn = (tc_q @ tc_k.transpose(-2, -1)) * self.scale
        if trg_mask:
            ones = torch.ones_like(tc_attn).to(self.device)
            dec_mask = torch.triu(ones, diagonal=1)
            tc_attn = tc_attn.masked_fill(dec_mask == 1, -1e9)
        tc_attn = tc_attn.softmax(dim=-1)
        tc_attn = self.tc_attn_drop(tc_attn)
        tc_x = (tc_attn @ tc_v).transpose(2, 3).reshape(B, N, T_q, D).transpose(1, 2)

        tc_x = self.norm_tatt1(tc_x + x_q)

        t_q = self.t_q_conv(tc_x).transpose(1, 2)
        t_k = self.t_k_conv(tc_x).transpose(1, 2)
        t_v = self.t_v_conv(tc_x).transpose(1, 2)
        t_q = t_q.reshape(B, N, T_q, self.t_num_heads, self.head_dim).permute(0, 1, 3, 2, 4)
        t_k = t_k.reshape(B, N, T_k, self.t_num_heads, self.head_dim).permute(0, 1, 3, 2, 4)
        t_v = t_v.reshape(B, N, T_v, self.t_num_heads, self.head_dim).permute(0, 1, 3, 2, 4)

        t_attn = (t_q @ t_k.transpose(-2, -1)) * self.scale
        if trg_mask:
            ones = torch.ones_like(t_attn).to(self.device)
            dec_mask = torch.triu(ones, diagonal=1)
            t_attn = t_attn.masked_fill(dec_mask == 1, -1e9)
        t_attn = t_attn.softmax(dim=-1)
        t_attn = self.t_attn_drop(t_attn)
        t_x = (t_attn @ t_v).transpose(2, 3).reshape(B, N, T_q, D).transpose(1, 2)

        t_x = self.norm_tatt2(t_x + tc_x)
        gcn_out = self.GCN(t_x, adj)
        x = self.proj_drop(gcn_out)
        return x


class STEncoderBlock(nn.Module):
    def __init__(
            self, dim, s_attn_size, t_attn_size, geo_num_heads=4, sem_num_heads=4, tc_num_heads=4, t_num_heads=4,
            mlp_ratio=4., qkv_bias=True, drop=0., attn_drop=0.,
            drop_path=0., act_layer=nn.GELU, device=torch.device('cpu'), type_ln="pre", output_dim=1,
    ):
        super().__init__()
        self.type_ln = type_ln
        self.norm1 = LlamaRMSNorm(dim)
        self.norm2 = LlamaRMSNorm(dim)
        self.st_attn = TemporalSelfAttention(dim, t_attn_size, t_num_heads=t_num_heads, tc_num_heads=tc_num_heads,
                                             qkv_bias=qkv_bias,
                                             attn_drop=attn_drop, proj_drop=drop, device=device)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = FeedForward(hidden_size=dim, intermediate_size=mlp_hidden_dim)

    def forward(self, x, dec_in, enc_out, TH, TP, adj, geo_mask=None, sem_mask=None):
        if self.type_ln == 'pre':
            x_nor1 = self.norm1(x)
            x = x + self.drop_path(
                self.st_attn(x_nor1, x_nor1, x_nor1, TH, TP, adj, geo_mask=geo_mask, sem_mask=sem_mask))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        elif self.type_ln == 'post':
            x = self.norm1(
                (x + self.drop_path(self.st_attn(x, x, x, TH, TP, adj, geo_mask=geo_mask, sem_mask=sem_mask))))
            x = self.norm2((x + self.drop_path(self.mlp(x))))
        else:
            x = x + self.drop_path(self.st_attn(x, x, x, TH, TP, adj, geo_mask=geo_mask, sem_mask=sem_mask))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class OpenCity(AbstractTrafficStateModel):
    def __init__(self, config, data_feature):
        super().__init__(config, data_feature)
        # section 1: data_feature
        self._scaler = self.data_feature.get('scaler')
        self.num_nodes = self.data_feature.get('num_nodes', 1)
        self.feature_dim = self.data_feature.get('feature_dim', 1)
        self.output_dim = self.data_feature.get('output_dim', 1)
        self._logger = getLogger()
        # section 2: model config
        self.input_window = config.get('input_window', 1)
        self.output_window = config.get('output_window', 1)
        self.device = config.get('device', torch.device('cpu'))
        self.hidden_size = config.get('hidden_size', 64)
        self.num_layers = config.get('num_layers', 1)
        self.dropout = config.get('dropout', 0)
        # section 3: model structure
        self.rnn = nn.LSTM(input_size=self.num_nodes * self.feature_dim, hidden_size=self.hidden_size,
                           num_layers=self.num_layers, dropout=self.dropout)
        self.fc = nn.Linear(self.hidden_size, self.num_nodes * self.output_dim)

    def predict(self, batch):
        pass

    def calculate_loss(self, batch):
        pass