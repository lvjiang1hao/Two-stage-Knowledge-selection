import torch.nn as nn
import torch
from .component import Encoder, Bridge, dot_attention, GeneralAttention, Decoder


class Seq2Seq(nn.Module):
    def __init__(self, config):
        super(Seq2Seq, self).__init__()

        self.config = config
        self.embedding = nn.Embedding(num_embeddings=config.vocab_size, embedding_dim=config.embed_size)

       
        self.utterance_encoder = Encoder(
            embedding=self.embedding,
            vocab_size=config.vocab_size,
            embed_size=config.embed_size,
            hidden_size=config.hidden_size,
            dropout=config.dropout
        )
       
        self.knowledge_encoder = Encoder(
            embedding=self.embedding,
            vocab_size=config.vocab_size,
            embed_size=config.embed_size,
            hidden_size=config.hidden_size,
            dropout=config.dropout
        )

        self.prior_net = GeneralAttention(query_size=config.hidden_size + config.embed_size, value_size=config.hidden_size)
        self.posterior_net = GeneralAttention(query_size=config.hidden_size * 2, value_size=config.hidden_size)

        self.bridge = Bridge(
            hidden_size=config.hidden_size
        )
        self.init_dec_state = nn.Linear(config.hidden_size + config.hidden_size, config.hidden_size)

        self.bow_fc1 = nn.Sequential(
            nn.Linear(config.hidden_size+config.hidden_size, config.hidden_size),
            nn.Tanh(),
            nn.Linear(config.hidden_size, config.vocab_size),
            nn.Softmax(dim=-1)
        )
        self.bow_fc2 = nn.Sequential(
            nn.Linear(config.hidden_size, config.hidden_size),
            nn.Tanh(),
            nn.Linear(config.hidden_size, config.vocab_size),
            nn.Softmax(dim=-1)
        )

        self.decoder = Decoder(
            embedding=self.embedding,
            vocab_size=config.vocab_size,
            embed_size=config.embed_size,
            hidden_size=config.hidden_size,
            dropout=config.dropout
        )

    def load_embedding(self):
        self.utterance_encoder.embedding.weight=self.embedding.weight
        self.knowledge_encoder.embedding.weight=self.embedding.weight
        self.decoder.embedding.weight = self.embedding.weight

    def forward(self, src_input, src_len, src_padding_mask,
                    kg_input, kg_len, kg_mask, kg_padding_mask, kg_extend_vocab, extra_zero,
                    tgt_input, tgt_len, is_train):
        # ??????????????????
        src_outputs, src_hidden = self.utterance_encoder(src_input, src_len)
        # ????????????
        kg_outputs, kg_hidden = self.knowledge_encoder(kg_input, kg_len)

        # ??????????????????????????????
        max_kg_num = kg_mask.size(-1)
        real_batch_size = src_outputs.size(0)
        kg_mask = kg_mask.unsqueeze(1)  # reshape to [b, 1, n]

        bridge_src_hidden = self.bridge(src_hidden)
        # ???????????????????????????
        kg_hidden = kg_hidden.reshape(real_batch_size, max_kg_num, -1)  # reshape to [B,N,H]
        # ??????
        top_src_hidden = bridge_src_hidden[-1].unsqueeze(1)

        pre_kg_fusion, _ = dot_attention(top_src_hidden, kg_hidden, kg_mask)
        pos_bow_dist = self.bow_fc1(torch.cat([top_src_hidden, pre_kg_fusion], dim=-1))

        pos_info = pos_bow_dist.squeeze(1).matmul(self.embedding.weight).unsqueeze(1)

        # ????????????
        kg_fusion, prior_dist = self.prior_net(torch.cat([top_src_hidden, pos_info], dim=-1), kg_hidden, kg_mask)


        # ????????????????????????????????????????????????
        kg_padding_mask = kg_padding_mask.unsqueeze(1)  # [b*n, 1, m] ??????knowledge outputs batch???????????????
        src_padding_mask = src_padding_mask.unsqueeze(1) # reshape to [b, 1, n]
        extra_zero = extra_zero.unsqueeze(1) # reshape to [b, 1, n]

        if is_train:
            # ?????????????????????????????????

            _, tgt_hidden = self.utterance_encoder(tgt_input[:, 1:], tgt_len - 1)  # ??????<sos>
            # ???????????????????????????????????????
            top_tgt_hidden = tgt_hidden[-1].unsqueeze(1)
            # ????????????
            kg_fusion, posterior_dist = self.posterior_net(
                torch.cat([top_src_hidden, top_tgt_hidden], dim=-1), kg_hidden, kg_mask)
            # ????????????????????????????????????
            posterior_dist = posterior_dist.detach()
            # ????????????
            bow_logits = self.bow_fc2(kg_fusion)
            # ??????????????????
            dec_init_hidden = self.init_dec_state(torch.cat([top_src_hidden, kg_fusion], dim=-1)).squeeze(1).unsqueeze(0)
            # ????????????

            decode_logits = self.decoder(tgt_input[:, :-1], dec_init_hidden, src_outputs, src_padding_mask,
                                            kg_hidden, kg_fusion, kg_outputs, kg_mask, kg_padding_mask, posterior_dist,
                                            kg_extend_vocab, extra_zero)

            return [bow_logits, decode_logits, prior_dist, posterior_dist]

        else:
            
            y_t = tgt_input[:, 0:1] # ???<sos>
            last_hidden = self.init_dec_state(torch.cat([top_src_hidden, kg_fusion], dim=-1)).squeeze(1).unsqueeze(0)
            pred_ids = []
            for i in range(self.config.max_dec_len):

                step_output, last_hidden = self.decoder(y_t, last_hidden, src_outputs, src_padding_mask, kg_hidden, kg_fusion, kg_outputs, kg_mask, kg_padding_mask, prior_dist, kg_extend_vocab, extra_zero,
                                                                    step_decode=True)

                probs, indices = step_output.max(dim=-1)
                # ????????????????????????????????????????????????????????????????????????????????????????????????????????????unk???id????????????????????????????????????????????????
                y_t = indices.masked_fill(mask=indices.gt(self.config.vocab_size-1), value=self.config.unk_id)

                pred_ids.append(indices)

            pred_ids = torch.cat(pred_ids, dim=1)

            return pred_ids, prior_dist.squeeze(1)

