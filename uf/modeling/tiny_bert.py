# coding:=utf-8
# Copyright 2020 Tencent. All rights reserved.
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
''' TinyBERT, a distillation model of BERT. '''

from uf.tools import tf
from .bert import BERTEncoder
from .base import BaseDecoder
from . import util


class TinyBERTCLSDistillor(BaseDecoder):
    def __init__(self,
                 student_config,
                 bert_config,
                 is_training,
                 input_ids,
                 input_mask,
                 segment_ids,
                 label_ids=None,
                 sample_weight=None,
                 scope='bert',
                 dtype=tf.float32,
                 drop_pooler=False,
                 label_size=2,
                 trainable=True,
                 **kwargs):
        super().__init__()

        def _get_logits(pooled_output, hidden_size, scope, trainable):
            with tf.variable_scope(scope):
                output_weights = tf.get_variable(
                    'output_weights',
                    shape=[label_size, hidden_size],
                    initializer=util.create_initializer(
                        bert_config.initializer_range),
                    trainable=trainable)
                output_bias = tf.get_variable(
                    'output_bias',
                    shape=[label_size],
                    initializer=tf.zeros_initializer(),
                    trainable=trainable)

                logits = tf.matmul(pooled_output,
                                   output_weights,
                                   transpose_b=True)
                logits = tf.nn.bias_add(logits, output_bias)
                return logits

        use_tilda_embedding=kwargs.get('use_tilda_embedding')
        student = BERTEncoder(
            bert_config=student_config,
            is_training=is_training,
            input_ids=input_ids,
            input_mask=input_mask,
            segment_ids=segment_ids,
            scope='tiny/bert',
            use_tilda_embedding=use_tilda_embedding,
            drop_pooler=drop_pooler,
            trainable=True,
            **kwargs)
        student_logits = _get_logits(
            student.get_pooled_output(),
            student_config.hidden_size, 'tiny/cls/seq_relationship', True)

        if is_training:
            teacher = BERTEncoder(
                bert_config=bert_config,
                is_training=False,
                input_ids=input_ids,
                input_mask=input_mask,
                segment_ids=segment_ids,
                scope=scope,
                use_tilda_embedding=False,
                drop_pooler=drop_pooler,
                trainable=False,
                **kwargs)
            teacher_logits = _get_logits(
                teacher.get_pooled_output(),
                bert_config.hidden_size, 'cls/seq_relationship', False)

            weights = 1.0
            if sample_weight is not None:
                weights = tf.cast(sample_weight, dtype=tf.float32)

            # embedding loss
            embedding_loss = self._get_embedding_loss(
                teacher, student, bert_config, weights)

            # attention loss
            attention_loss = self._get_attention_loss(
                teacher, student, bert_config, student_config, weights)

            # hidden loss
            hidden_loss = self._get_hidden_loss(
                teacher, student, bert_config, student_config, weights)

            # prediction loss
            pred_loss = self._get_pred_loss(
                teacher_logits, student_logits, weights)

            # sum up
            distill_loss = (embedding_loss + attention_loss +
                            hidden_loss + pred_loss)
            self.total_loss = distill_loss
            self.losses['losses'] = distill_loss

        else:
            student_probs = tf.nn.softmax(
                student_logits, axis=-1, name='probs')
            self.probs['probs'] = student_probs
            self.preds['preds'] = tf.argmax(student_probs, axis=-1)

    def _get_embedding_loss(self, teacher, student, bert_config, weights):
        teacher_embedding = teacher.get_embedding_output()
        teacher_embedding = tf.stop_gradient(teacher_embedding)
        student_embedding = student.get_embedding_output()
        with tf.variable_scope('embedding_loss'):
            linear_trans = tf.layers.dense(
                student_embedding,
                bert_config.hidden_size,
                kernel_initializer=util.create_initializer(
                    bert_config.initializer_range))
            embedding_loss = tf.losses.mean_squared_error(
                linear_trans, teacher_embedding,
                weights=tf.reshape(weights, [-1, 1, 1]))
        return embedding_loss

    def _get_attention_loss(self, teacher, student,
                            bert_config, student_config, weights):
        teacher_attention_scores = teacher.get_attention_scores()
        teacher_attention_scores = [tf.stop_gradient(value)
                                    for value in teacher_attention_scores]
        student_attention_scores = student.get_attention_scores()
        num_teacher_hidden_layers = bert_config.num_hidden_layers
        num_student_hidden_layers = student_config.num_hidden_layers
        num_projections = \
            int(num_teacher_hidden_layers / num_student_hidden_layers)
        attention_losses = []
        for i in range(num_student_hidden_layers):
            attention_losses.append(tf.losses.mean_squared_error(
                teacher_attention_scores[
                    num_projections * i + num_projections - 1],
                student_attention_scores[i],
                weights=tf.reshape(weights, [-1, 1, 1, 1])),)
        attention_loss = tf.add_n(attention_losses)
        return attention_loss

    def _get_hidden_loss(self, teacher, student,
                         bert_config, student_config, weights):
        teacher_hidden_layers = teacher.all_encoder_layers
        teacher_hidden_layers = [tf.stop_gradient(value)
                                 for value in teacher_hidden_layers]
        student_hidden_layers = student.all_encoder_layers
        num_teacher_hidden_layers = bert_config.num_hidden_layers
        num_student_hidden_layers = student_config.num_hidden_layers
        num_projections = int(
            num_teacher_hidden_layers / num_student_hidden_layers)
        with tf.variable_scope('hidden_loss'):
            hidden_losses = []
            for i in range(num_student_hidden_layers):
                hidden_losses.append(tf.losses.mean_squared_error(
                    teacher_hidden_layers[
                        num_projections * i + num_projections - 1],
                    tf.layers.dense(
                        student_hidden_layers[i], bert_config.hidden_size,
                        kernel_initializer=util.create_initializer(
                            bert_config.initializer_range)),
                    weights=tf.reshape(weights, [-1, 1, 1])))
            hidden_loss = tf.add_n(hidden_losses)
        return hidden_loss

    def _get_pred_loss(self, teacher_logits, student_logits, weights):
        teacher_probs = tf.nn.softmax(teacher_logits, axis=-1)
        teacher_probs = tf.stop_gradient(teacher_probs)
        student_log_probs = tf.nn.log_softmax(student_logits, axis=-1)
        pred_loss = (
            - tf.reduce_sum(teacher_probs * student_log_probs, axis=-1) *
            tf.reshape(weights, [-1, 1]))
        pred_loss = tf.reduce_mean(pred_loss)
        return pred_loss
