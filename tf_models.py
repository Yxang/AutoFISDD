from abc import abstractmethod
from itertools import combinations
import numpy as np
import tensorflow as tf
import bisect
import copy

import __init__
from tf_utils import row_col_fetch, row_col_expand, batch_kernel_product, \
    batch_mlp, create_placeholder, drop_out, embedding_lookup, linear, output, bin_mlp, get_variable, \
    layer_normalization, batch_normalization, get_l2_loss, split_data_mask

dtype = __init__.config['dtype']

if dtype.lower() == 'float32' or dtype.lower() == 'float':
    dtype = tf.float32
elif dtype.lower() == 'float64':
    dtype = tf.float64

class Model:
    inputs = None
    outputs = None
    logits = None
    labels = None
    learning_rate = None
    loss = None
    l2_loss = None
    optimizer = None
    grad = None

    @abstractmethod
    def compile(self, **kwargs):
        pass

    def __str__(self):
        return self.__class__.__name__

def generate_pairs(ranges=range(1, 100), mask=None, order=2):
    res = []
    for i in range(order):
        res.append([])
    for i, pair in enumerate(list(combinations(ranges, order))):
        if mask is None or mask[i] == 1:
            for j in range(order):
                res[j].append(pair[j])
    print("generated pairs", len(res[0]))
    return res

class AutoFM(Model):
    def __init__(self, init='xavier', num_inputs=None, input_dim=None, embed_size=None, l2_w=None, l2_v=None,
                 norm=False, real_inputs=None, comb_mask=None, weight_base=0.6, third_prune=False, 
                 comb_mask_third=None, weight_base_third=0.6, retrain_stage=0, prune_threshold=0.3):
        self.l2_w = l2_w if not retrain_stage else 0.
        self.l2_v = l2_v if not retrain_stage else 0.
        self.l2_ps = l2_v if not retrain_stage else 0.
        self.third_prune = third_prune
        self.retrain_stage = retrain_stage
        self.prune_threshold = prune_threshold

        self.inputs, self.labels, self.training = create_placeholder(num_inputs, tf, True)

        inputs, mask, flag, num_inputs = split_data_mask(self.inputs, num_inputs, norm=norm, real_inputs=real_inputs)

        self.xw, self.xv, b, _ = embedding_lookup(init=init, input_dim=input_dim, factor=embed_size, inputs=inputs,
                                               apply_mask=flag, mask=mask, third_order=third_prune)

        l = linear(self.xw)
        self.cols, self.rows = generate_pairs(range(self.xv.shape[1]),mask=comb_mask)
        t_embedding_matrix = tf.transpose(self.xv, perm=[1, 0, 2])
        left = tf.transpose(tf.gather(t_embedding_matrix, self.rows), perm=[1, 0, 2])
        right = tf.transpose(tf.gather(t_embedding_matrix, self.cols), perm=[1, 0, 2])
        level_2_matrix = tf.reduce_sum(tf.multiply(left, right), axis=-1)
        with tf.variable_scope("edge_weight", reuse=tf.AUTO_REUSE):
            self.edge_weights = tf.get_variable('weights', shape=[len(self.cols)],
                                                initializer=tf.random_uniform_initializer(
                                                minval=weight_base - 0.001,
                                                maxval=weight_base + 0.001))
            normed_wts = tf.identity(self.edge_weights, name="normed_wts")
            tf.add_to_collection("structure", self.edge_weights)
            tf.add_to_collection("edge_weights", self.edge_weights)
            mask = tf.identity(normed_wts, name="unpruned_mask")
            mask = tf.expand_dims(mask, axis=0)
        level_2_matrix = tf.layers.batch_normalization(level_2_matrix, axis=-1, training=self.training,
                                                    reuse=tf.AUTO_REUSE, scale=True, center=True, name='prune_BN')
        level_2_matrix *= mask                                          
        if third_prune:
            self.first, self.second, self.third = generate_pairs(range(self.xv.shape[1]), mask=comb_mask_third, order=3)
            t_embedding_matrix = tf.transpose(self.xv, perm=[1, 0, 2])
            first_embed = tf.transpose(tf.gather(t_embedding_matrix, self.first), perm=[1, 0, 2])
            second_embed = tf.transpose(tf.gather(t_embedding_matrix, self.second), perm=[1, 0, 2])
            third_embed = tf.transpose(tf.gather(t_embedding_matrix, self.third), perm=[1, 0, 2])
            level_3_matrix = tf.reduce_sum(tf.multiply(tf.multiply(first_embed, second_embed), third_embed), axis=-1)
            with tf.variable_scope("third_edge_weight", reuse=tf.AUTO_REUSE):
                self.third_edge_weights = tf.get_variable('third_weights', shape=[len(self.first)],
                                                          initializer=tf.random_uniform_initializer(
                                                              minval=weight_base_third - 0.001,
                                                              maxval=weight_base_third + 0.001))
                third_normed_wts = tf.identity(self.third_edge_weights, name="third_normed_wts")
                tf.add_to_collection("third_structure", self.third_edge_weights)
                tf.add_to_collection("third_edge_weights", self.third_edge_weights)
                third_mask = tf.identity(third_normed_wts, name="third_unpruned_mask")
                third_mask = tf.expand_dims(third_mask, axis=0)
            level_3_matrix = tf.layers.batch_normalization(level_3_matrix, axis=-1, training=self.training,
                                                           reuse=tf.AUTO_REUSE, scale=True, center=True,
                                                           name="level_3_matrix_BN")
            level_3_matrix *= third_mask

        fm_out = tf.reduce_sum(level_2_matrix, axis=-1)
        if third_prune:
            fm_out2 = tf.reduce_sum(level_3_matrix, axis=-1)
        if third_prune:
            self.logits, self.outputs = output([l, fm_out,fm_out2, b, ])
        else:
            self.logits, self.outputs = output([l, fm_out, b, ])

    def pick_feature(self, weights, order, lower_weights=None):
        assert order >= 2
        if lower_weights is not None:
            assert order >= 3

        max_bound = np.max(np.abs(weights))
        weights = np.sign(weights) * np.abs(weights) / max_bound
        if lower_weights is not None:
            max_bound = np.max(np.abs(lower_weights))
            lower_weights = np.sign(lower_weights) * np.abs(lower_weights) / max_bound
            weights = np.sign(weights) * np.abs(weights) / max_bound
        else:
            max_bound = np.max(np.abs(weights))
            weights = np.sign(weights) * np.abs(weights) / max_bound
        combs = generate_pairs(range(self.xv.shape[1]), mask=None, order=order)
        combs = list(zip(*combs))
        if order >= 3:
            lower_combs = generate_pairs(range(self.xv.shape[1]), mask=None, order=order - 1)
            lower_combs = list(zip(*lower_combs))
        mask = np.ones_like(weights, dtype=np.int)
        pruned = 0
        for i, (weight, comb) in enumerate(zip(weights, combs)):
            if np.abs(weight) < self.prune_threshold:
                mask[i] = 0
                continue
            else:
                if order >= 3:
                    for i_removed in range(order):
                        lower_comb = list(comb).copy()
                        lower_comb.pop(i_removed)
                        lower_comb = tuple(lower_comb)
                        id_lower_comb = bisect.bisect_left(lower_combs, lower_comb)
                        w_lower_comb = lower_weights[id_lower_comb]
                        if 0.8 * np.abs(weight) < np.abs(w_lower_comb):
                            mask[i] = 0
                            pruned += 1
                            break
        print(f'''
#####################
{pruned}
#####################        
''')

        return mask

    def analyse_structure(self, sess, print_full_weight=False, epoch=None):
        import numpy as np
        wts = sess.run("edge_weight/normed_wts:0")
        if not self.retrain_stage:
            mask = self.pick_feature(wts, order=2)
        if print_full_weight:
            outline = ""
            for j in range(wts.shape[0]):
                outline += str(wts[j]) + ","
            outline += "\n"
            print("log avg auc all weights for(epoch:%s)" % (epoch), outline)

            outline = ""
            if not self.retrain_stage:
                for j in range(wts.shape[0]):
                    outline += str(mask[j]) + ","
                outline += "\n"
                print("log avg auc all masks for(epoch:%s)" % (epoch), outline)
        print("wts", wts[:10])
        if not self.retrain_stage:
            print("mask", mask[:10])
            zeros_ = np.zeros_like(mask, dtype=np.float32)
            zeros_[mask == 0] = 1
            print("masked edge_num", sum(zeros_))
        if self.third_prune:
            wts_3 = sess.run("third_edge_weight/third_normed_wts:0")
            if not self.retrain_stage:
                mask = self.pick_feature(wts_3, order=3, lower_weights=wts)
            if print_full_weight:
                if not self.retrain_stage:
                    outline = ""
                    for j in range(wts_3.shape[0]):
                        outline += str(mask[j]) + ","
                    outline += "\n"
                    print("third log avg auc all third weights for(epoch:%s)" % (epoch), outline)
            print("third wts", wts_3[:10])
            if not self.retrain_stage:
                print("third mask", mask[:10])
                zeros_ = np.zeros_like(mask, dtype=np.float32)
                zeros_[mask == 0] = 1
                print("third masked edge_num", sum(zeros_))

    def compile(self, loss=None, optimizer1=None, optimizer2=None, global_step=None, pos_weight=1.0):
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            with tf.name_scope('loss'):
                self.loss = tf.reduce_mean(loss(logits=self.logits, targets=self.labels, pos_weight=pos_weight))
                _loss_ = self.loss
                if self.third_prune:
                    self.l2_loss = get_l2_loss([self.l2_w, self.l2_v],
                                               [self.xw, self.xv])
                else:
                    self.l2_loss = get_l2_loss([self.l2_w, self.l2_v],
                                               [self.xw, self.xv])
                if self.l2_loss is not None:
                    _loss_ += self.l2_loss
                all_variable = [v for v in tf.trainable_variables()]
                self.optimizer1 = optimizer1.minimize(loss=_loss_, var_list=all_variable)

class AutoDeepFM(Model):
    def __init__(self, init='xavier', num_inputs=None, input_dim=None, embed_size=None, l2_w=None, l2_v=None,
                 layer_sizes=None, layer_acts=None, layer_keeps=None, layer_l2=None, norm=False, real_inputs=None,
                 batch_norm=False, layer_norm=False, comb_mask=None, weight_base=0.6, third_prune=False, 
                 comb_mask_third=None, weight_base_third=0.6, retrain_stage=0, prune_threshold=0.3):
        self.l2_w = l2_w if not retrain_stage else 0.
        self.l2_v = l2_v if not retrain_stage else 0.
        self.l2_ps = l2_v if not retrain_stage else 0.
        self.layer_l2 = layer_l2 if not retrain_stage else 0.
        self.retrain_stage = retrain_stage
        self.prune_threshold = prune_threshold

        self.inputs, self.labels, self.training = create_placeholder(num_inputs, tf, True)
        layer_keeps = drop_out(self.training, layer_keeps)
        inputs, mask, flag, num_inputs = split_data_mask(self.inputs, num_inputs, norm=norm, real_inputs=real_inputs)

        self.xw, xv, _, _ = embedding_lookup(init=init, input_dim=input_dim, factor=embed_size, inputs=inputs,
                                            apply_mask=flag, mask=mask, use_b=False, third_order=third_prune)
        self.third_prune = third_prune
        self.xv = xv
        h = tf.reshape(xv, [-1, num_inputs * embed_size])
        h, self.layer_kernels, _ = bin_mlp(init, layer_sizes, layer_acts, layer_keeps, h, num_inputs * embed_size,
                                           batch_norm=batch_norm, layer_norm=layer_norm, training=self.training)
        h = tf.squeeze(h)

        l = linear(self.xw)
        self.cols, self.rows = generate_pairs(range(self.xv.shape[1]),mask=comb_mask)
        t_embedding_matrix = tf.transpose(self.xv, perm=[1, 0, 2])
        left = tf.transpose(tf.gather(t_embedding_matrix, self.rows), perm=[1, 0, 2])
        right = tf.transpose(tf.gather(t_embedding_matrix, self.cols), perm=[1, 0, 2])
        level_2_matrix = tf.reduce_sum(tf.multiply(left, right), axis=-1)
        with tf.variable_scope("edge_weight", reuse=tf.AUTO_REUSE):
            self.edge_weights = tf.get_variable('weights', shape=[len(self.cols)],
                                                initializer=tf.random_uniform_initializer(
                                                minval=weight_base - 0.001,
                                                maxval=weight_base + 0.001))
            normed_wts = tf.identity(self.edge_weights, name="normed_wts")
            tf.add_to_collection("structure", self.edge_weights)
            tf.add_to_collection("edge_weights", self.edge_weights)
            mask = tf.identity(normed_wts, name="unpruned_mask")
            mask = tf.expand_dims(mask, axis=0)
        level_2_matrix = tf.layers.batch_normalization(level_2_matrix, axis=-1, training=self.training,
                                                    reuse=tf.AUTO_REUSE, scale=True, center=True, name='prune_BN')
        level_2_matrix *= mask                                          
        if third_prune:
            self.first, self.second, self.third = generate_pairs(range(self.xv.shape[1]), mask=comb_mask_third, order=3)
            t_embedding_matrix = tf.transpose(self.xv, perm=[1, 0, 2])
            first_embed = tf.transpose(tf.gather(t_embedding_matrix, self.first), perm=[1, 0, 2])
            second_embed = tf.transpose(tf.gather(t_embedding_matrix, self.second), perm=[1, 0, 2])
            third_embed = tf.transpose(tf.gather(t_embedding_matrix, self.third), perm=[1, 0, 2])
            level_3_matrix = tf.reduce_sum(tf.multiply(tf.multiply(first_embed, second_embed), third_embed), axis=-1)
            with tf.variable_scope("third_edge_weight", reuse=tf.AUTO_REUSE):
                self.third_edge_weights = tf.get_variable('third_weights', shape=[len(self.first)],
                                                          initializer=tf.random_uniform_initializer(
                                                              minval=weight_base_third - 0.001,
                                                              maxval=weight_base_third + 0.001))
                third_normed_wts = tf.identity(self.third_edge_weights, name="third_normed_wts")
                tf.add_to_collection("third_structure", self.third_edge_weights)
                tf.add_to_collection("third_edge_weights", self.third_edge_weights)
                third_mask = tf.identity(third_normed_wts, name="third_unpruned_mask")
                third_mask = tf.expand_dims(third_mask, axis=0)
            level_3_matrix = tf.layers.batch_normalization(level_3_matrix, axis=-1, training=self.training,
                                                           reuse=tf.AUTO_REUSE, scale=True, center=True,
                                                           name="level_3_matrix_BN")
            level_3_matrix *= third_mask

        fm_out = tf.reduce_sum(level_2_matrix, axis=-1)
        if third_prune:
            fm_out2 = tf.reduce_sum(level_3_matrix, axis=-1)
        if third_prune:
            self.logits, self.outputs = output([l, fm_out,fm_out2, h, ])
        else:
            self.logits, self.outputs = output([l, fm_out, h, ])

    def analyse_structure(self, sess, print_full_weight=False, epoch=None):
        import numpy as np
        wts = sess.run("edge_weight/normed_wts:0")
        if not self.retrain_stage:
            mask = self.pick_feature(wts, order=2)
        if print_full_weight:
            outline = ""
            for j in range(wts.shape[0]):
                outline += str(wts[j]) + ","
            outline += "\n"
            print("log avg auc all weights for(epoch:%s)" % (epoch), outline)

            outline = ""
            if not self.retrain_stage:
                for j in range(wts.shape[0]):
                    outline += str(mask[j]) + ","
                outline += "\n"
                print("log avg auc all masks for(epoch:%s)" % (epoch), outline)
        print("wts", wts[:10])
        if not self.retrain_stage:
            print("mask", mask[:10])
            zeros_ = np.zeros_like(mask, dtype=np.float32)
            zeros_[mask == 0] = 1
            print("masked edge_num", sum(zeros_))
        if self.third_prune:
            wts_3 = sess.run("third_edge_weight/third_normed_wts:0")
            if not self.retrain_stage:
                mask = self.pick_feature(wts_3, order=3, lower_weights=wts)
            if print_full_weight:
                if not self.retrain_stage:
                    outline = ""
                    for j in range(wts_3.shape[0]):
                        outline += str(mask[j]) + ","
                    outline += "\n"
                    print("third log avg auc all third weights for(epoch:%s)" % (epoch), outline)
            print("third wts", wts_3[:10])
            if not self.retrain_stage:
                print("third mask", mask[:10])
                zeros_ = np.zeros_like(mask, dtype=np.float32)
                zeros_[mask == 0] = 1
                print("third masked edge_num", sum(zeros_))

    def pick_feature(self, weights, order, lower_weights=None):
        assert order >= 2
        if lower_weights is not None:
            assert order >= 3

        max_bound, min_bound = max(np.abs(weights)), min(np.abs(weights))
        weights = np.sign(weights) * (np.abs(weights) - min_bound) / (max_bound - min_bound)
        if lower_weights is not None:
            max_bound, min_bound = max(np.abs(lower_weights)), min(np.abs(lower_weights))
            lower_weights = np.sign(lower_weights) * (np.abs(lower_weights) - min_bound) / (max_bound - min_bound)
            weights = np.where(np.abs(weights) >= min_bound, weights, 0.)
            weights = np.sign(weights) * (np.abs(weights) - min_bound) / (max_bound - min_bound)
        else:
            max_bound, min_bound = max(np.abs(weights)), min(np.abs(weights))
            weights = np.sign(weights) * (np.abs(weights) - min_bound) / (max_bound - min_bound)
        combs = generate_pairs(range(self.xv.shape[1]), mask=None, order=order)
        combs = list(zip(*combs))
        if order >= 3:
            lower_combs = generate_pairs(range(self.xv.shape[1]), mask=None, order=order - 1)
            lower_combs = list(zip(*lower_combs))
        mask = np.ones_like(weights, dtype=np.int)
        pruned = 0
        for i, (weight, comb) in enumerate(zip(weights, combs)):
            if np.abs(weight) < self.prune_threshold:
                mask[i] = 0
                continue
            else:
                if order >= 3:
                    for i_removed in range(order):
                        lower_comb = list(comb).copy()
                        lower_comb.pop(i_removed)
                        lower_comb = tuple(lower_comb)
                        id_lower_comb = bisect.bisect_left(lower_combs, lower_comb)
                        w_lower_comb = lower_weights[id_lower_comb]
                        if 0.8 * np.abs(weight) < np.abs(w_lower_comb):
                            mask[i] = 0
                            pruned += 1
                            break
        print(f'''
#####################
{pruned}
#####################        
''')

        return mask

    def compile(self, loss=None, optimizer1=None, optimizer2=None, global_step=None, pos_weight=1.0):
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            with tf.name_scope('loss'):
                self.loss = tf.reduce_mean(loss(logits=self.logits, targets=self.labels, pos_weight=pos_weight))
                _loss_ = self.loss
                if self.third_prune:
                    self.l2_loss = get_l2_loss([self.l2_w, self.l2_v, self.layer_l2],
                                               [self.xw, self.xv, self.layer_kernels])
                else:
                    self.l2_loss = get_l2_loss([self.l2_w, self.l2_v, self.layer_l2],
                                               [self.xw, self.xv, self.layer_kernels])
                if self.l2_loss is not None:
                    _loss_ += self.l2_loss
                all_variable = [v for v in tf.trainable_variables()]
                self.optimizer1 = optimizer1.minimize(loss=_loss_, var_list=all_variable)
