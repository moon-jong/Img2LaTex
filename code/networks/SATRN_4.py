import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math
import random
import timm

from dataset import START, PAD

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class DenseNetV2(nn.Module):
    def __init__(self, dropout_rate=0.2):
        super(DenseNetV2, self).__init__()
        self.conv = timm.create_model('resnetrs152', pretrained=True, num_classes=0, global_pool='', drop_rate=0.25)
        self.trans1_norm = nn.BatchNorm2d(2048)
        self.trans1_relu = nn.ReLU(inplace=True)
        self.trans1_conv = nn.Conv2d(
            2048, 1024, kernel_size=1, stride=1, bias=False  # 128
        )
        self.trans2_norm = nn.BatchNorm2d(1024)
        self.trans2_relu = nn.ReLU(inplace=True)
        self.trans2_conv = nn.Conv2d(
            1024, 256, kernel_size=1, stride=1, bias=False  # 128
        )
    def forward(self, x):
        x = self.conv(x)
        x = self.trans1_norm(x)
        x = self.trans1_relu(x)
        x = self.trans1_conv(x)
        x = self.trans2_norm(x)
        x = self.trans2_relu(x)
        x = self.trans2_conv(x)
        return x

class BottleneckBlock(nn.Module):
    """
    Dense Bottleneck Block

    It contains two convolutional layers, a 1x1 and a 3x3.
    """

    def __init__(self, input_size, growth_rate, dropout_rate=0.2, num_bn=3):
        """
        Args:
            input_size (int): Number of channels of the input
            growth_rate (int): Number of new features being added. That is the ouput
                size of the last convolutional layer.
            dropout_rate (float, optional): Probability of dropout [Default: 0.2]
        """
        super(BottleneckBlock, self).__init__()
        inter_size = num_bn * growth_rate
        self.norm1 = nn.BatchNorm2d(input_size)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(
            input_size, inter_size, kernel_size=1, stride=1, bias=False
        )
        self.norm2 = nn.BatchNorm2d(inter_size)
        self.conv2 = nn.Conv2d(
            inter_size, growth_rate, kernel_size=3, stride=1, padding=1, bias=False
        )
        self.dropout = nn.Dropout(dropout_rate)

    def forward(self, x):
        out = self.conv1(self.relu(self.norm1(x)))
        out = self.conv2(self.relu(self.norm2(out)))
        out = self.dropout(out)
        return torch.cat([x, out], 1)


class TransitionBlock(nn.Module):
    """
    Transition Block

    A transition layer reduces the number of feature maps in-between two bottleneck
    blocks.
    """

    def __init__(self, input_size, output_size):
        """
        Args:
            input_size (int): Number of channels of the input
            output_size (int): Number of channels of the output
        """
        super(TransitionBlock, self).__init__()
        self.norm = nn.BatchNorm2d(input_size)
        self.relu = nn.ReLU(inplace=True)
        self.conv = nn.Conv2d(
            input_size, output_size, kernel_size=1, stride=1, bias=False
        )
        self.pool = nn.AvgPool2d(kernel_size=2, stride=2)

    def forward(self, x):
        out = self.conv(self.relu(self.norm(x)))
        return self.pool(out)


class DenseBlock(nn.Module):
    """
    Dense block

    A dense block stacks several bottleneck blocks.
    """

    def __init__(self, input_size, growth_rate, depth, dropout_rate=0.2):
        """
        Args:
            input_size (int): Number of channels of the input
            growth_rate (int): Number of new features being added per bottleneck block
            depth (int): Number of bottleneck blocks
            dropout_rate (float, optional): Probability of dropout [Default: 0.2]
        """
        super(DenseBlock, self).__init__()
        layers = [
            BottleneckBlock(
                input_size + i * growth_rate, growth_rate, dropout_rate=dropout_rate
            )
            for i in range(depth)
        ]
        self.block = nn.Sequential(*layers)

    def forward(self, x):
        return self.block(x)


class DeepCNN300(nn.Module):
    """
    This is specialized to the math formula recognition task
    - three convolutional layers to reduce the visual feature map size and to capture low-level visual features
    (128, 256) -> (8, 32) -> total 256 features
    - transformer layers cannot change the channel size, so it requires a wide feature dimension
    ***** this might be a point to be improved !!
    """

    def __init__(
            self, input_channel, num_in_features, output_channel=256, dropout_rate=0.2, depth=16, growth_rate=24
    ):
        super(DeepCNN300, self).__init__()
        self.conv0 = nn.Conv2d(
            input_channel,  # 3
            num_in_features,  # 48
            kernel_size=7,
            stride=2,
            padding=3,
            bias=False,
        )
        self.norm0 = nn.BatchNorm2d(num_in_features)
        self.relu = nn.ReLU(inplace=True)
        self.max_pool = nn.MaxPool2d(
            kernel_size=2, stride=2
        )  # 1/4 (128, 128) -> (32, 32)
        num_features = num_in_features

        self.block1 = DenseBlock(
            num_features,  # 48
            growth_rate=growth_rate,  # 48 + growth_rate(24)*depth(16) -> 432
            depth=depth,  # 16?
            dropout_rate=dropout_rate,
        )
        num_features = num_features + depth * growth_rate
        self.trans1 = TransitionBlock(num_features, num_features // 2)  # 16 x 16
        num_features = num_features // 2
        self.block2 = DenseBlock(
            num_features,  # 128
            growth_rate=growth_rate,  # 16
            depth=depth,  # 8
            dropout_rate=dropout_rate,
        )
        num_features = num_features + depth * growth_rate
        self.trans2_norm = nn.BatchNorm2d(num_features)
        self.trans2_relu = nn.ReLU(inplace=True)
        self.trans2_conv = nn.Conv2d(
            num_features, num_features // 2, kernel_size=1, stride=1, bias=False  # 128
        )

    def forward(self, input):
        out = self.conv0(input)  # (H, V, )
        out = self.relu(self.norm0(out))
        out = self.max_pool(out)
        out = self.block1(out)
        out = self.trans1(out)
        out = self.block2(out)
        out_before_trans2 = self.trans2_relu(self.trans2_norm(out))
        out_A = self.trans2_conv(out_before_trans2)
        return out_A  # 128 x (16x16)


class ScaledDotProductAttention(nn.Module):
    def __init__(self, temperature, dropout=0.1):
        super(ScaledDotProductAttention, self).__init__()

        self.temperature = temperature
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, q, k, v, mask=None, prev=None):
        if prev is not None:
            attn = torch.matmul(q, k.transpose(2, 3)) / self.temperature + prev
        else:
            attn = torch.matmul(q, k.transpose(2, 3)) / self.temperature
        if mask is not None:
            attn = attn.masked_fill(mask=mask, value=float("-inf"))
        prev = attn
        attn = torch.softmax(attn, dim=-1)
        attn = self.dropout(attn)
        out = torch.matmul(attn, v)
        return out, prev
    
class MultiheadAttentionEncoder(nn.Module):

    def __init__(self, input_dim, embed_dim, num_heads, dropout=0.1):
        super(MultiheadAttentionEncoder, self).__init__()
        assert embed_dim % num_heads == 0, "Embedding dimension must be 0 modulo number of heads."

        self.input_dim = input_dim
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads

        # Stack all weight matrices 1...h together for efficiency
        # Note that in many implementations you see "bias=False" which is optional
        self.qkv_proj = nn.Linear(self.input_dim, 3*self.embed_dim)
        self.o_proj = nn.Linear(self.embed_dim, self.embed_dim)
        self.attention = ScaledDotProductAttention(
            temperature=(self.embed_dim) ** 0.5, dropout=dropout
        )

#         self._reset_parameters()

    def _reset_parameters(self):
        # Original Transformer initialization, see PyTorch documentation
        nn.init.xavier_uniform_(self.qkv_proj.weight)
        self.qkv_proj.bias.data.fill_(0)
        nn.init.xavier_uniform_(self.o_proj.weight)
        self.o_proj.bias.data.fill_(0)


    def forward(self, x, mask=None, return_attention=False, prev=None):
        batch_size, seq_length, embed_dim = x.size()
        qkv = self.qkv_proj(x)

        # Separate Q, K, V from linear output
        qkv = qkv.reshape(batch_size, seq_length, self.num_heads, 3*self.head_dim)
        qkv = qkv.permute(0, 2, 1, 3) # [Batch, Head, SeqLen, Dims]
        q, k, v = qkv.chunk(3, dim=-1)

        # Determine value outputs
        values, attention = self.attention(q, k, v, mask=mask, prev=prev)
        # out shape 변경
        values = values.permute(0, 2, 1, 3) # [Batch, SeqLen, Head, Dims]
        values = values.reshape(batch_size, seq_length, embed_dim)
        o = self.o_proj(values) # out = self.out_linear(out)
        # 수정 전
        if return_attention:
            return o, attention
        else:
            return o

class MultiHeadAttention(nn.Module):
    def __init__(self, q_channels, k_channels, head_num=8, dropout=0.1):
        super(MultiHeadAttention, self).__init__()

        self.q_channels = q_channels
        self.k_channels = k_channels
        self.head_dim = q_channels // head_num  # 300//8 == 37.5 -> 37
        self.head_num = head_num

        self.q_linear = nn.Linear(q_channels, q_channels)
        self.k_linear = nn.Linear(k_channels, q_channels)
        self.v_linear = nn.Linear(k_channels, q_channels)
        self.attention = ScaledDotProductAttention(
            temperature=(q_channels) ** 0.5, dropout=dropout
        )
        self.out_linear = nn.Linear(self.head_num * self.head_dim, q_channels)
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, q, k, v, mask=None):
        b, q_len, k_len, v_len = q.size(0), q.size(1), k.size(1), v.size(1)
        q = (
            self.q_linear(q)
                .view(b, q_len, self.head_num, self.head_dim)
                .transpose(1, 2)
        )
        k = (
            self.k_linear(k)
                .view(b, k_len, self.head_num, self.head_dim)
                .transpose(1, 2)
        )
        v = (
            self.v_linear(v)
                .view(b, v_len, self.head_num, self.head_dim)
                .transpose(1, 2)
        )

        if mask is not None:
            mask = mask.unsqueeze(1)

        out, attn = self.attention(q, k, v, mask=mask)
        out = (
            out.transpose(1, 2)
                .contiguous()
                .view(b, q_len, self.head_num * self.head_dim)
        )
        out = self.out_linear(out)
        out = self.dropout(out)

        return out


class Feedforward(nn.Module):
    def __init__(self, filter_size=2048, hidden_dim=512, dropout=0.1, encode=None):
        super(Feedforward, self).__init__()

        # Convolution
        # Reference : https://github.com/Media-Smart/vedastr/blob/0fd2a0bd7819ae4daa2a161501e9f1c2ac67e96a/configs/small_satrn.py
        if encode == True:
            self.layers = nn.Sequential(
                nn.Conv2d(in_channels=hidden_dim, out_channels=filter_size, kernel_size=3, padding=1, bias=True),
                nn.ReLU(True),
                nn.Dropout(p=dropout),
                nn.Conv2d(in_channels=filter_size, out_channels=hidden_dim, kernel_size=3, padding=1, bias=True),
                # nn.ReLU(True),
                nn.Dropout(p=dropout),
            )
        else:
            self.layers = nn.Sequential(
                nn.Linear(hidden_dim, filter_size, True),
                nn.ReLU(True),
                nn.Dropout(p=dropout),
                nn.Linear(filter_size, hidden_dim, True),
                #nn.ReLU(True),
                nn.Dropout(p=dropout),
            )

        # Separable
        # Reference : https://github.com/clovaai/SATRN/blob/master/src/networks/SATRN.py
        # if encode == True:
        #     self.layers = nn.Sequential(
        #         nn.Conv2d(in_channels=hidden_dim, out_channels=filter_size, kernel_size=1, padding=0, bias=True),
        #         nn.ReLU(True),
        #         nn.Dropout(p=dropout),
        #         nn.Conv2d(in_channels=filter_size, out_channels=filter_size, kernel_size=3, padding=1, bias=True),
        #         nn.ReLU(True),
        #         nn.Dropout(p=dropout),
        #         nn.Conv2d(in_channels=filter_size, out_channels=hidden_dim, kernel_size=1, padding=0, bias=True),
        #         nn.ReLU(True),
        #         nn.Dropout(p=dropout),
        #     )
        # else:
        #     self.layers = nn.Sequential(
        #         nn.Linear(hidden_dim, filter_size, True),
        #         nn.ReLU(True),
        #         nn.Dropout(p=dropout),
        #         nn.Linear(filter_size, hidden_dim, True),
        #         nn.ReLU(True),
        #         nn.Dropout(p=dropout),
        #     )

    def forward(self, input):
        return self.layers(input)


class TransformerEncoderLayer(nn.Module):
    def __init__(self, input_size, filter_size, head_num, dropout_rate=0.2):
        super(TransformerEncoderLayer, self).__init__()

        self.attention_layer = MultiheadAttentionEncoder(
            input_dim=input_size,
            embed_dim=input_size,
            num_heads=head_num,
            dropout=dropout_rate,
        )
        self.attention_norm = nn.LayerNorm(normalized_shape=input_size)
        self.feedforward_layer = Feedforward(
            filter_size=filter_size, hidden_dim=input_size, encode=True
        )
        self.feedforward_norm = nn.LayerNorm(normalized_shape=input_size)

    def forward(self, input, shape, prev=None):
        B, C, H, W = shape
        att, prev = self.attention_layer(input, return_attention=True, prev=prev)
        # input = input.transpose(1, 2).view(B, C, H, W)  # [b, h x w, c]
        out = self.attention_norm(att + input)  # [B, H*W, C]
        out_ff = out.transpose(1, 2).view(B, C, H, W)  # [B, C, H, W]
        ff = self.feedforward_layer(out_ff)  # [B, C, H, W]
        ff = ff.view(B, C, H * W).transpose(1, 2)  # [B, C, H*W]
        out = self.feedforward_norm(ff + out)
        return out, prev


class PositionalEncoding2D(nn.Module):
    def __init__(self, in_channels, max_h=64, max_w=128, dropout=0.1):
        super(PositionalEncoding2D, self).__init__()

        self.h_position_encoder = self.generate_encoder(in_channels // 2, max_h)
        self.w_position_encoder = self.generate_encoder(in_channels // 2, max_w)

        self.h_linear = nn.Linear(in_channels // 2, in_channels // 2)
        self.w_linear = nn.Linear(in_channels // 2, in_channels // 2)

        self.h_scale = self.scale_factor_generate(in_channels)
        self.w_scale = self.scale_factor_generate(in_channels)

        self.dropout = nn.Dropout(p=dropout)
        self.pool = nn.AdaptiveAvgPool2d(1)

    def generate_encoder(self, in_channels, max_len):
        pos = torch.arange(max_len).float().unsqueeze(1)
        i = torch.arange(in_channels).float().unsqueeze(0)
        angle_rates = 1 / torch.pow(10000, (2 * (i // 2)) / in_channels)
        position_encoder = pos * angle_rates
        position_encoder[:, 0::2] = torch.sin(position_encoder[:, 0::2])
        position_encoder[:, 1::2] = torch.cos(position_encoder[:, 1::2])
        return position_encoder  # (Max_len, In_channel)

    def scale_factor_generate(self, in_channels):
        scale_factor = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(in_channels, in_channels, kernel_size=1),
            nn.Sigmoid()
        )

        return scale_factor

    def forward(self, input):
        ### Require DEBUG
        b, c, h, w = input.size()
        h_pos_encoding = (
            self.h_position_encoder[:h, :].unsqueeze(1).to(input.get_device())
        )  # [H, 1, D]
        h_pos_encoding = self.h_linear(h_pos_encoding)  # [H, 1, C/2]

        w_pos_encoding = (
            self.w_position_encoder[:w, :].unsqueeze(0).to(input.get_device())
        )  # [1, W, D]
        w_pos_encoding = self.w_linear(w_pos_encoding)  # [1, W, C/2]

        h_pos_encoding = h_pos_encoding.expand(-1, w, -1)  # H, W, C/2
        w_pos_encoding = w_pos_encoding.expand(h, -1, -1)  # H, W, C/2

        pos_encoding = torch.cat([h_pos_encoding, w_pos_encoding], dim=2)  # [H, W, C]
        pos_encoding = pos_encoding.permute(2, 0, 1)  # [C, H, W]
        pos_encoding = pos_encoding.expand(b, -1, -1, -1)  # [B, C, H, W]

        pooled = self.pool(input)  # [B, C, 1, 1]
        h_sclaed = self.h_scale(pooled)  # [B, C, 1, 1]
        w_sclaed = self.w_scale(pooled)  # [B, C, 1, 1]

        pos_h = h_sclaed * pos_encoding[:, :, :h, :]  # alpha
        pos_w = w_sclaed * pos_encoding[:, :, :, :w]  # beta

        out = input + pos_h + pos_w  # [B, C, H ,W]
        out = self.dropout(out)

        return out


class TransformerEncoderFor2DFeatures(nn.Module):
    """
    Transformer Encoder for Image
    1) ShallowCNN : low-level visual feature identification and dimension reduction
    2) Positional Encoding : adding positional information to the visual features
    3) Transformer Encoders : self-attention layers for the 2D feature maps
    """

    def __init__(
            self,
            input_size,
            hidden_dim,
            filter_size,
            head_num,
            layer_num,
            dropout_rate=0.1,
            checkpoint=None,
    ):
        super(TransformerEncoderFor2DFeatures, self).__init__()

        self.shallow_cnn = DenseNetV2()
        self.positional_encoding = PositionalEncoding2D(hidden_dim)
        self.attention_layers = nn.ModuleList(
            [
                TransformerEncoderLayer(hidden_dim, filter_size, head_num, dropout_rate)
                for _ in range(layer_num)
            ]
        )
        if checkpoint is not None:
            self.load_state_dict(checkpoint)

    def forward(self, input):

        out = self.shallow_cnn(input)  # [b, c, h, w]
        out = self.positional_encoding(out)  # [b, c, h, w]

        # flatten
        b, c, h, w = out.size()
        shape = [b, c, h, w]
        out = out.view(b, c, h * w).transpose(1, 2)  # [b, h x w, c]
        prev = None
        for layer in self.attention_layers:
            out, prev = layer(out, shape, prev)
        return out


class TransformerDecoderLayer(nn.Module):
    def __init__(self, input_size, src_size, filter_size, head_num, dropout_rate=0.2):
        super(TransformerDecoderLayer, self).__init__()

        self.self_attention_layer = MultiHeadAttention(
            q_channels=input_size,
            k_channels=input_size,
            head_num=head_num,
            dropout=dropout_rate,
        )
        self.self_attention_norm = nn.LayerNorm(normalized_shape=input_size)

        self.attention_layer = MultiHeadAttention(
            q_channels=input_size,
            k_channels=src_size,
            head_num=head_num,
            dropout=dropout_rate,
        )
        self.attention_norm = nn.LayerNorm(normalized_shape=input_size)

        self.feedforward_layer = Feedforward(
            filter_size=filter_size, hidden_dim=input_size
        )
        self.feedforward_norm = nn.LayerNorm(normalized_shape=input_size)

    def forward(self, tgt, tgt_prev, src, tgt_mask):

        if tgt_prev == None:  # Train
            att = self.self_attention_layer(tgt, tgt, tgt, tgt_mask)
            out = self.self_attention_norm(att + tgt)

            att = self.attention_layer(att, src, src)
            out = self.attention_norm(att + out)

            ff = self.feedforward_layer(out)
            out = self.feedforward_norm(ff + out)
        else:
            tgt_prev = torch.cat([tgt_prev, tgt], 1)
            att = self.self_attention_layer(tgt, tgt_prev, tgt_prev, tgt_mask)
            out = self.self_attention_norm(att + tgt)

            att = self.attention_layer(att, src, src)
            out = self.attention_norm(att + out)

            ff = self.feedforward_layer(out)
            out = self.feedforward_norm(ff + out)
        return out


class PositionEncoder1D(nn.Module):
    def __init__(self, in_channels, max_len=500, dropout=0.1):
        super(PositionEncoder1D, self).__init__()

        self.position_encoder = self.generate_encoder(in_channels, max_len)
        self.position_encoder = self.position_encoder.unsqueeze(0)
        self.dropout = nn.Dropout(p=dropout)

    def generate_encoder(self, in_channels, max_len):
        pos = torch.arange(max_len).float().unsqueeze(1)

        i = torch.arange(in_channels).float().unsqueeze(0)
        angle_rates = 1 / torch.pow(10000, (2 * (i // 2)) / in_channels)

        position_encoder = pos * angle_rates
        position_encoder[:, 0::2] = torch.sin(position_encoder[:, 0::2])
        position_encoder[:, 1::2] = torch.cos(position_encoder[:, 1::2])

        return position_encoder

    def forward(self, x, point=-1):
        if point == -1:
            out = x + self.position_encoder[:, : x.size(1), :].to(x.get_device())
            out = self.dropout(out)
        else:
            out = x + self.position_encoder[:, point, :].unsqueeze(1).to(x.get_device())
        return out


class TransformerDecoder(nn.Module):
    def __init__(
            self,
            num_classes,
            src_dim,
            hidden_dim,
            filter_dim,
            head_num,
            dropout_rate,
            pad_id,
            st_id,
            layer_num=1,
            checkpoint=None,
    ):
        super(TransformerDecoder, self).__init__()

        self.embedding = nn.Embedding(num_classes + 1, hidden_dim)
        self.hidden_dim = hidden_dim
        self.filter_dim = filter_dim
        self.num_classes = num_classes
        self.layer_num = layer_num

        self.pos_encoder = PositionEncoder1D(
            in_channels=hidden_dim, dropout=dropout_rate
        )

        self.attention_layers = nn.ModuleList(
            [
                TransformerDecoderLayer(
                    hidden_dim, src_dim, filter_dim, head_num, dropout_rate
                )
                for _ in range(layer_num)
            ]
        )
        self.generator = nn.Linear(hidden_dim, num_classes)

        self.pad_id = pad_id
        self.st_id = st_id

        if checkpoint is not None:
            self.load_state_dict(checkpoint)

    def pad_mask(self, text):
        pad_mask = text == self.pad_id
        pad_mask[:, 0] = False
        pad_mask = pad_mask.unsqueeze(1)

        return pad_mask

    def order_mask(self, length):
        order_mask = torch.triu(torch.ones(length, length), diagonal=1).bool()
        order_mask = order_mask.unsqueeze(0).to(device)
        return order_mask

    def text_embedding(self, texts):
        tgt = self.embedding(texts)
        tgt *= math.sqrt(tgt.size(2))

        return tgt

    def forward(
            self, src, text, is_train=True, batch_max_length=50, teacher_forcing_ratio=1.0
    ):

        if is_train and random.random() < teacher_forcing_ratio:
            tgt = self.text_embedding(text)
            tgt = self.pos_encoder(tgt)
            tgt_mask = self.pad_mask(text) | self.order_mask(text.size(1))
            for layer in self.attention_layers:
                tgt = layer(tgt, None, src, tgt_mask)
            out = self.generator(tgt)
        else:
            out = []
            num_steps = batch_max_length - 1
            target = torch.LongTensor(src.size(0)).fill_(self.st_id).to(device)  # [START] token
            features = [None] * self.layer_num

            for t in range(num_steps):
                target = target.unsqueeze(1)
                tgt = self.text_embedding(target)
                tgt = self.pos_encoder(tgt, point=t)
                tgt_mask = self.order_mask(t + 1)
                tgt_mask = tgt_mask[:, -1].unsqueeze(1)  # [1, (l+1)]
                for l, layer in enumerate(self.attention_layers):
                    tgt = layer(tgt, features[l], src, tgt_mask)
                    features[l] = (
                        tgt if features[l] == None else torch.cat([features[l], tgt], 1)
                    )

                _out = self.generator(tgt)  # [b, 1, c]
                target = torch.argmax(_out[:, -1:, :], dim=-1)  # [b, 1]
                target = target.squeeze()  # [b]
                out.append(_out)

            out = torch.stack(out, dim=1).to(device)  # [b, max length, 1, class length]
            out = out.squeeze(2)  # [b, max length, class length]

        return out


class SATRN_4(nn.Module):
    def __init__(self, FLAGS, train_dataset, checkpoint=None):
        super(SATRN_4, self).__init__()

        self.encoder = TransformerEncoderFor2DFeatures(
            input_size=FLAGS.data.rgb,
            hidden_dim=FLAGS.SATRN.encoder.hidden_dim,
            filter_size=FLAGS.SATRN.encoder.filter_dim,
            head_num=FLAGS.SATRN.encoder.head_num,
            layer_num=FLAGS.SATRN.encoder.layer_num,
            dropout_rate=FLAGS.dropout_rate,
        )

        self.decoder = TransformerDecoder(
            num_classes=len(train_dataset.id_to_token),
            src_dim=FLAGS.SATRN.decoder.src_dim,
            hidden_dim=FLAGS.SATRN.decoder.hidden_dim,
            filter_dim=FLAGS.SATRN.decoder.filter_dim,
            head_num=FLAGS.SATRN.decoder.head_num,
            dropout_rate=FLAGS.dropout_rate,
            pad_id=train_dataset.token_to_id[PAD],
            st_id=train_dataset.token_to_id[START],
            layer_num=FLAGS.SATRN.decoder.layer_num,
        )

        self.criterion = (
            nn.CrossEntropyLoss()
        )  # without ignore_index=train_dataset.token_to_id[PAD]

        self.apply(self._init_weights)

        if checkpoint:
            self.load_state_dict(checkpoint)

    def _init_weights(self, module):
        if isinstance(module, (nn.Linear, nn.Embedding)):
            module.weight.data.normal_(mean=0.0, std=0.02)
            if isinstance(module, nn.Linear) and module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
            
    def forward(self, input, expected, is_train, teacher_forcing_ratio):
        enc_result = self.encoder(input)
        dec_result = self.decoder(
            enc_result,
            expected[:, :-1],
            is_train,
            expected.size(1),
            teacher_forcing_ratio,
        )
        return dec_result
