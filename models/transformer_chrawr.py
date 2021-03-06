# coding=utf-8
# Copyright 2017 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""transformer (attention).

encoder: [Self-Attention, Feed-forward] x n
decoder: [Self-Attention, Source-Target-Attention, Feed-forward] x n
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

from tensor2tensor.layers import common_layers
from tensor2tensor.layers import common_attention
from tensor2tensor.utils import registry
from tensor2tensor.models import transformer

import tensorflow as tf


@registry.register_model
class TransformerChrawr(transformer.Transformer):
  """Transformer with Character-Aware Embedding."""

  def encode(self, inputs, target_space, hparams):
    """Encode transformer inputs.

    Args:
      inputs: Transformer inputs [batch_size, input_length, hidden_dim]
      target_space: scalar, target space ID.
      hparams: hyperparmeters for model.

    Returns:
      Tuple of:
          encoder_output: Encoder representation.
              [batch_size, input_length, hidden_dim]
          encoder_decoder_attention_bias: Bias and mask weights for
              encodre-decoder attention. [batch_size, input_length]
    """
    inputs = common_layers.flatten4d3d(inputs)
    
    ### Character-Aware Embedding ###
    inputs = chrawr_embedding(inputs, hparams)

    encoder_input, self_attention_bias, encoder_decoder_attention_bias = (
        transformer.transformer_prepare_encoder(inputs, target_space, hparams))

    encoder_input = tf.nn.dropout(
        encoder_input, 1.0 - hparams.layer_prepostprocess_dropout)

    encoder_output = transformer.transformer_encoder(
        encoder_input,
        self_attention_bias,
        hparams)

    return encoder_output, encoder_decoder_attention_bias

def chrawr_embedding(emb, hparams):
    emb_mask = embedding_mask(emb)
    tf.summary.image('emb_mask', tf.expand_dims(emb_mask[:32], 0))

    # rescale dimension(depth)
    emb = tf.layers.conv1d(emb, hparams.reduced_input_size, 1, 1, 'same', name="rescaled_embedding")
    if hparams.chr_pos_enc:
        emb = common_attention.add_timing_signal_1d(emb)
    emb = emb * emb_mask
    

    # chracter aware convolution
    emb = conv_emb(emb, hparams, emb_mask)
    emb_mask_in = embedding_mask(emb)
    tf.summary.image('emb_mask_in', tf.expand_dims(emb_mask_in[:32], 0))
    emb = tf.nn.dropout(emb, 1.0 - hparams.chr_dropout_rate)

    emb = highway(emb, emb.get_shape()[-1], hparams)
    emb = emb * emb_mask_in
    encoder_input = tf.nn.dropout(emb, 1.0 - hparams.chr_dropout_rate)

    # restore dimension(depth)
    emb = tf.layers.conv1d(emb, hparams.hidden_size, 1, 1, 'same', name="restored_embedding")
    emb = emb * emb_mask_in

    return emb

def embedding_mask(emb):
    emb_sum = tf.reduce_sum(tf.abs(emb), axis=-1)
    return tf.expand_dims(tf.to_float(tf.not_equal(emb_sum, 0.0)), -1)

def highway(inputs, size, hparams, bias=-2.0, f=tf.nn.relu, scope='Highway'):
    """Highway Network (cf. http://arxiv.org/abs/1505.00387).
    t = sigmoid(Wy + b)
    z = t * g(Wy + b) + (1 - t) * y
    where g is nonlinearity, t is transform gate, and (1 - t) is carry gate.
    """

    with tf.variable_scope(scope):
        for idx in range(hparams.num_highway_layers):
            t = tf.layers.conv1d(inputs, size, 1, 1, 'same', bias_initializer=tf.constant_initializer(bias), activation=tf.nn.sigmoid, name='highway_lin_%d' % idx)
            g = tf.layers.conv1d(inputs, size, 1, 1, 'same', activation=hparams.chr_nonlinearity, name='highway_gate_%d' % idx)

            output = t * g + (1. - t) * inputs
            inputs = output

    return output


def conv_emb(inputs, hparams, input_mask, scope='ConvEmb'):
    '''
    :inputs:           input float tensor of shape [(batch_size) x time_step x embed_size]
    :kernels:         array of kernel sizes
    :kernel_features: array of kernel feature sizes (parallel to kernels)
    '''
    assert len(hparams.chr_kernels) == len(hparams.chr_kernel_features), 'Kernel and Features must have the same size'

    layers = []
    with tf.variable_scope(scope):
        for kernel_size, kernel_feature_size in zip(hparams.chr_kernels, hparams.chr_kernel_features):

            # [batch_size x time_step x kernel_feature_size]
            conv = tf.layers.conv1d(inputs, kernel_feature_size, kernel_size, 1, 'same', activation=hparams.chr_nonlinearity, name="kernel_%d" % kernel_size)
            conv = conv * input_mask # remove calculate values for zero padding

            # [batch_size x modified_time_step x kernel_feature_size]
            pool = tf.layers.max_pooling1d(conv, hparams.chr_maxpool_size, hparams.chr_maxpool_size, 'same', name="kernel_%d_pool" % kernel_size)

            layers.append(pool)
        if len(hparams.chr_kernels) > 1:
            output = tf.concat(layers, 2)
        else:
            output = layers[0]

    return output # [batch_size x modified_time_step x hidden_dim]

@registry.register_hparams
def transformer_chrawr_base():
  """Base hparams for Transformer with Character Aware Embedding."""
  hparams = transformer.transformer_base()
  hparams.num_highway_layers = 4
  hparams.reduced_input_size = 128
  hparams.hidden_size = 512
  hparams.chr_kernels = [1,2,3,4,5,6,7,8]
  hparams.chr_kernel_features = [200,200,250,250,300,300,300,300]
  hparams.chr_maxpool_size = 5
  hparams.chr_nonlinearity = tf.nn.tanh
  hparams.chr_dropout_rate = 0.
  hparams.chr_pos_enc = False

  hparams.target_modality="symbol:tgtemb"

  return hparams

@registry.register_hparams
def transformer_chrawr_big():
  """HParams for transfomer_chrawr big model on WMT."""
  hparams = transformer_chrawr_base()
  hparams.hidden_size = 1024
  hparams.filter_size = 4096
  hparams.num_heads = 16
  hparams.layer_prepostprocess_dropout = 0.3
  return hparams


@registry.register_hparams
def transformer_chrawr_big_single_gpu():
  """HParams for transformer_chrawr big model for single gpu."""
  hparams = transformer_chrawr_big()
  hparams.layer_prepostprocess_dropout = 0.1
  hparams.learning_rate_warmup_steps = 16000
  hparams.optimizer_adam_beta2 = 0.998
  return hparams


@registry.register_hparams
def transformer_chrawr_base_single_gpu():
  """HParams for transformer_chrawr base model for single gpu."""
  hparams = transformer_chrawr_base()
  hparams.batch_size = 2048
  hparams.learning_rate_warmup_steps = 16000
  return hparams


# For Fast Test
@registry.register_hparams
def transformer_chrawr_l2():
  hparams = transformer_chrawr_base()
  hparams.num_hidden_layers = 2
  return hparams

@registry.register_hparams
def transformer_chrawr_test0():
  hparams = transformer_chrawr_l2()
  return hparams

@registry.register_hparams
def transformer_chrawr_test1(): # small kernel, small pooling
  hparams = transformer_chrawr_l2()
  hparams.chr_kernels = [1,2,3,4,5]
  hparams.chr_kernel_features = [250,250,300,300,300]
  hparams.chr_maxpool_size = 3
  return hparams

@registry.register_hparams
def transformer_chrawr_test2(): # small kernel
  hparams = transformer_chrawr_l2()
  hparams.chr_kernels = [1,2,3,4,5]
  hparams.chr_kernel_features = [250,250,300,300,300]
  return hparams


@registry.register_hparams
def transformer_chrawr_test3(): # small pooling
  hparams = transformer_chrawr_l2()
  hparams.chr_maxpool_size = 3
  return hparams

@registry.register_hparams
def transformer_chrawr_test4(): # small highway
  hparams = transformer_chrawr_l2()
  hparams.num_highway_layers = 1
  return hparams

@registry.register_hparams
def transformer_chrawr_test5(): # non target emb sharing
  hparams = transformer_chrawr_l2()
  hparams.target_modality="default"
  return hparams

@registry.register_hparams
def transformer_chrawr_test6(): # relu
  hparams = transformer_chrawr_l2()
  hparams.chr_nonlinearity = tf.nn.relu
  return hparams

@registry.register_hparams
def transformer_chrawr_test7(): # elu
  hparams = transformer_chrawr_l2()
  hparams.chr_nonlinearity = tf.nn.elu
  return hparams

@registry.register_hparams
def transformer_chrawr_test8(): # dropout
  hparams = transformer_chrawr_l2()
  hparams.chr_dropout_rate = .2
  return hparams

@registry.register_hparams
def transformer_chrawr_test9(): # positional_encoding
  hparams = transformer_chrawr_l2()
  hparams.chr_pos_enc = True
  return hparams

@registry.register_hparams
def transformer_chrawr_test10(): # more small pooling
  hparams = transformer_chrawr_l2()
  hparams.chr_maxpool_size = 2
  return hparams

@registry.register_hparams
def transformer_chrawr_test11(): # more small pooling & more small kernels
  hparams = transformer_chrawr_l2()
  hparams.chr_maxpool_size = 2
  hparams.chr_kernels = [1,2,3,4,5]
  hparams.chr_kernel_features = [250,250,300,300,300]
  return hparams

@registry.register_hparams
def transformer_chrawr_test12(): # more small pooling & positional_encoding
  hparams = transformer_chrawr_l2()
  hparams.chr_maxpool_size = 2
  hparams.chr_pos_enc = True
  return hparams

@registry.register_hparams
def transformer_chrawr_test13(): # more small pooling & more small kernels & positional_encoding
  hparams = transformer_chrawr_l2()
  hparams.chr_maxpool_size = 2
  hparams.chr_kernels = [1,2,3,4,5]
  hparams.chr_kernel_features = [250,250,300,300,300]
  hparams.chr_pos_enc = True
  return hparams

@registry.register_hparams
def transformer_chrawr_test14(): 
  # more small pooling & more small kernels & positional_encoding & small highway & relu &non target emb sharing
  hparams = transformer_chrawr_l2()
  hparams.chr_maxpool_size = 2
  hparams.chr_kernels = [1,2,3,4,5]
  hparams.chr_kernel_features = [250,250,300,300,300]
  hparams.chr_pos_enc = True
  hparams.num_highway_layers = 1
  hparams.chr_nonlinearity = tf.nn.relu
  hparams.target_modality="default"
  return hparams

### MOS ###
@registry.register_hparams
def transformer_mos():
  hparams = transformer.transformer_base()
  hparams.n_experts = 15
  hparams.target_modality="symbol:mos"
  return hparams

@registry.register_hparams
def transformer_mos_single_gpu():
  hparams = transformer.transformer_base_single_gpu()
  hparams.n_experts = 15
  hparams.target_modality="symbol:mos"
  return hparams

@registry.register_hparams
def transformer_chrawr_mos():
  hparams = transformer_chrawr_base()
  hparams.n_experts = 15
  hparams.target_modality="symbol:mos"
  return hparams

@registry.register_hparams
def transformer_chrawr_mos_single_gpu():
  hparams = transformer_chrawr_base_single_gpu()
  hparams.n_experts = 15
  hparams.target_modality="symbol:mos"
  return hparams

### USELESS ###
@registry.register_hparams
def transformer_chrawr_long_single_gpu():
  """HParams for transformer_chrawr model for single gpu."""
  hparams = transformer_chrawr_base_single_gpu()
  hparams.chr_maxpool_size = 1
  return hparams

@registry.register_hparams
def transformer_chrawr_many_single_gpu():
  """HParams for transformer_chrawr model for single gpu."""
  hparams = transformer_chrawr_base_single_gpu()
  hparams.batch_size = 2 * hparams.batch_size
  hparams.chr_kernel_features = [224,224,224,224,224,224,224,224]
  hparams.chr_maxpool_size = 3
  return hparams


@registry.register_hparams
def transformer_chrawr_general_single_gpu():
  """HParams for transformer_chrawr model for single gpu."""
  hparams = transformer_chrawr_base_single_gpu()
  hparams.chr_kernel_features = [224,224,224,224,224,224,224,224]
  hparams.chr_maxpool_size = 3
  return hparams

@registry.register_hparams
def transformer_chrawr_general_long_single_gpu():
  """HParams for transformer_chrawr model for single gpu."""
  hparams = transformer_chrawr_base_single_gpu()
  hparams.chr_kernel_features = [224,224,224,224,224,224,224,224]
  hparams.chr_maxpool_size = 1
  return hparams
