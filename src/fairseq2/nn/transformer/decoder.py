# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import math
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, Optional, final

import torch.nn as nn
import torch.nn.functional as F
from overrides import final as finaloverride, override
from torch import Tensor
from torch.nn import LayerNorm, Module

from fairseq2.nn.embedding import Embedding
from fairseq2.nn.incremental_state import IncrementalStateBag
from fairseq2.nn.module_list import ModuleList
from fairseq2.nn.positional_embedding import PositionalEmbedding
from fairseq2.nn.projection import Projection, ResettableProjection
from fairseq2.nn.transformer.attention_mask import (
    AttentionMaskGenerator,
    CausalAttentionMaskGenerator,
)
from fairseq2.nn.transformer.decoder_layer import TransformerDecoderLayer
from fairseq2.nn.transformer.norm_order import TransformerNormOrder
from fairseq2.nn.utils import to_float_mask
from fairseq2.typing import DataType, Device


class TransformerDecoder(Module, ABC):
    """Represents a Transformer decoder."""

    model_dim: int
    """The dimensionality of the model (i.e. inputs and outputs)."""

    def __init__(self, model_dim: int) -> None:
        """
        :param model_dim:
            The dimensionality of the model (i.e. inputs and outputs).
        """
        super().__init__()

        self.model_dim = model_dim

    @abstractmethod
    def forward(
        self,
        seq: Tensor,
        enc_out: Optional[Tensor] = None,
        enc_padding_mask: Optional[Tensor] = None,
        state_bag: Optional[IncrementalStateBag] = None,
    ) -> Tensor:
        """
        :param seq:
            The target sequences. *Shape:* :math:`(N,T)`, or :math:`(T)` when
            unbatched, where :math:`N` is the batch size and :math:`T` is the
            target sequence length.
        :param enc_out:
            The encoder output for the encoder-decoder attention. *Shape:*
            :math:`(N,S,M_{enc})`, or :math:`(S,M_{enc})` when unbatched, where
            :math:`N` is the batch size, :math:`S` is the source sequence
            length, and :math:`M_{enc}` is the encoder model size.
        :param enc_padding_mask:
            The boolean or float key padding mask indicating which key positions
            to ignore for the purpose of encoder-decoder attention. *Shape:*
            :math:`(N,S)`, or :math:`(S)` when unbatched, where :math:`N` is the
            batch size and :math:`S` is the source sequence length.
        :param state_bag:
            The state bag to use during an incremental evaluation.

        :returns:
            The output. *Shape:* :math:`(N,T,M)`, or :math:`(T,M)` when
            unbatched, where :math:`N` is the batch size, :math:`T` is the
            target sequence length, and :math:`M` is the model size.

        .. note::
            For a boolean key padding mask, a ``True`` indicates that the
            corresponding key position is not allowed to attend. For a float key
            padding mask, the mask values will be added to the attention
            weights.
        """

    def extra_repr(self) -> str:
        """:meta private:"""
        return f"model_dim={self.model_dim}"


class InternalDimProjection(ResettableProjection):
    def __init__(
        self,
        inp_dim: int,
        out_dim: int,
        device: Optional[Device],
        dtype: Optional[DataType],
    ) -> None:
        super().__init__(inp_dim, out_dim, bias=True, device=device, dtype=dtype)

    @override
    def reset_parameters(self) -> None:
        nn.init.xavier_uniform_(self.weight)

        if self.bias is not None:
            nn.init.zeros_(self.bias)


@final
class StandardTransformerDecoder(TransformerDecoder):
    """Represents a Transformer decoder layer as described in
    :cite:t:`DBLP:journals/corr/VaswaniSPUJGKP17`."""

    embed: Embedding
    embed_scale: float
    pos_embed: Optional[PositionalEmbedding]
    embed_norm: Optional[LayerNorm]
    embed_dropout_p: float
    inp_dim_proj: Optional[Projection]
    self_attn_mask_gen: AttentionMaskGenerator
    layers: ModuleList
    layer_norm: Optional[LayerNorm]
    out_dim_proj: Optional[Projection]

    def __init__(
        self,
        embed: Embedding,
        pos_embed: Optional[PositionalEmbedding],
        layers: Iterable[TransformerDecoderLayer],
        no_scale_embed: bool = False,
        norm_embed: bool = False,
        embed_dropout_p: float = 0.1,
        self_attn_mask_gen: Optional[AttentionMaskGenerator] = None,
        layer_drop_p: float = 0.0,
        norm_order: TransformerNormOrder = TransformerNormOrder.POST,
        norm_eps: float = 1e-5,
        device: Optional[Device] = None,
        dtype: Optional[DataType] = None,
    ) -> None:
        """
        :param embed:
            The output embedding dictionary.
        :param pos_embed:
            The positional embedding dictionary.
        :param layers:
            The decoder layers.
        :param no_scale_embed:
            If ``True``, output embeddings won't be scaled by the square root
            of the embedding size.
        :param norm_embed:
            If ``True``, applies Layer Normalization to the sum of output and
            positional embeddings.
        :param embed_dropout_p:
            The dropout probability on output embeddings.
        :param self_attn_mask_gen:
            The attention mask generator. If ``None``, an instance of
            :class:`CausalAttentionMaskGenerator` will be used.
        :param layer_drop_p:
            If greater than zero, applies LayerDrop to the decoder layers as
            described in :cite:t:`DBLP:journals/corr/abs-1909-11556`.
        :param norm_order:
            The Layer Normalization order to use.
        :param norm_eps:
            The epsilon value to add to the denominator of the
            :class:`~torch.nn.LayerNorm` modules for numerical stability.
        """
        fct_kwargs: Dict[str, Any] = {"device": device, "dtype": dtype}

        layer_list = ModuleList(layers, layer_drop_p)
        if not layer_list:
            raise ValueError("`layers` must contain at least one decoder layer.")

        model_dim = layer_list[0].model_dim

        for idx, layer in enumerate(layers):
            if layer.model_dim != model_dim:
                raise ValueError(
                    f"`model_dim` of the decoder layer {idx} ({layer.model_dim}) does not match `model_dim` ({model_dim})."
                )

        super().__init__(model_dim)

        embedding_dim = embed.embedding_dim

        self.embed = embed

        self.embed_scale = 1.0 if no_scale_embed else math.sqrt(embedding_dim)

        if pos_embed is not None:
            if pos_embed.embedding_dim != embedding_dim:
                raise ValueError(
                    f"`embedding_dim` of `pos_embed` ({pos_embed.embedding_dim}) does not match `embedding_dim` of `embed` ({embedding_dim})."
                )

            self.pos_embed = pos_embed
        else:
            self.register_module("pos_embed", None)

        if norm_embed:
            self.embed_norm = LayerNorm(embedding_dim, norm_eps, **fct_kwargs)
        else:
            self.register_module("embed_norm", None)

        self.embed_dropout_p = embed_dropout_p

        if embedding_dim != model_dim:
            self.inp_dim_proj = InternalDimProjection(
                embedding_dim, model_dim, **fct_kwargs
            )
        else:
            self.register_module("inp_dim_proj", None)

        if self_attn_mask_gen is None:
            self.self_attn_mask_gen = CausalAttentionMaskGenerator()
        else:
            self.self_attn_mask_gen = self_attn_mask_gen

        self.layers = layer_list

        if norm_order != TransformerNormOrder.POST:
            self.layer_norm = LayerNorm(model_dim, norm_eps, **fct_kwargs)
        else:
            self.register_module("layer_norm", None)

        if embedding_dim != model_dim:
            self.out_dim_proj = InternalDimProjection(
                model_dim, embedding_dim, **fct_kwargs
            )
        else:
            self.register_module("out_dim_proj", None)

    @finaloverride
    def forward(
        self,
        seq: Tensor,
        enc_out: Optional[Tensor] = None,
        enc_padding_mask: Optional[Tensor] = None,
        state_bag: Optional[IncrementalStateBag] = None,
    ) -> Tensor:
        self_attn_padding_mask = self._get_self_attn_padding_mask(seq)

        x = self._forward_embed(seq, state_bag)

        if self.inp_dim_proj is not None:
            x = self.inp_dim_proj(x)

        x = self._forward_decoder_layers(
            x,
            self_attn_padding_mask,
            enc_out,
            enc_padding_mask,
            state_bag,
        )

        if self.out_dim_proj is not None:
            x = self.out_dim_proj(x)

        return x

    def _get_self_attn_padding_mask(self, seq: Tensor) -> Optional[Tensor]:
        if self.embed.padding_idx is not None:
            mask = seq.eq(self.embed.padding_idx)

            # Applying a reduction (i.e. `any()`) and returning `None` if the
            # mask does not contain any padding sounds like a smart idea, but
            # doing so causes a device-to-host transfer which costs more time
            # than applying the mask in a fused op (i.e. `baddbmm`).

            return to_float_mask(mask, dtype=self.embed.weight.dtype)
        else:
            return None

    def _forward_embed(
        self, seq: Tensor, state_bag: Optional[IncrementalStateBag]
    ) -> Tensor:
        embed = self.embed(seq)

        if self.embed_scale != 1.0:
            embed = embed * self.embed_scale

        if self.pos_embed is not None:
            embed = self.pos_embed(embed, state_bag)

        if self.embed_norm is not None:
            embed = self.embed_norm(embed)

        if self.embed_dropout_p > 0.0:
            embed = F.dropout(embed, self.embed_dropout_p, self.training)

        return embed  # type: ignore[no-any-return]

    def _forward_decoder_layers(
        self,
        x: Tensor,
        self_attn_padding_mask: Optional[Tensor],
        enc_out: Optional[Tensor],
        enc_padding_mask: Optional[Tensor],
        state_bag: Optional[IncrementalStateBag],
    ) -> Tensor:
        self_attn_mask: Optional[Tensor] = None

        if self.training or state_bag is None:
            self_attn_mask = self.self_attn_mask_gen(x)

        for layer in self.layers:
            x = layer(
                x,
                self_attn_mask,
                self_attn_padding_mask,
                enc_out,
                enc_padding_mask,
                state_bag,
            )

        if self.layer_norm is not None:
            x = self.layer_norm(x)

        return x


@final
class ScoreProjection(ResettableProjection):
    """Produces scores (i.e. logits) from the output of a Transformer decoder.

    The produced scores should be forwarded to a softmax function to compute
    predicted next-token probabilities.
    """

    def __init__(
        self,
        num_embed: int,
        embedding_dim: int,
        device: Optional[Device] = None,
        dtype: Optional[DataType] = None,
    ) -> None:
        """
        :param num_embed:
            The size of the output embedding dictionary.
        :param embedding_dim:
            The dimensionality of output embeddings.
        """
        super().__init__(
            embedding_dim, num_embed, bias=False, device=device, dtype=dtype
        )

    @finaloverride
    def reset_parameters(self) -> None:
        """Resets the parameters and buffers of the module."""
        nn.init.normal_(self.weight, std=self.inp_dim**-0.5)

        if self.bias is not None:
            nn.init.zeros_(self.bias)
