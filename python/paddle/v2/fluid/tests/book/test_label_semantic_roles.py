import numpy as np
import paddle.v2 as paddle
import paddle.v2.dataset.conll05 as conll05
import paddle.v2.fluid.core as core
import paddle.v2.fluid.framework as framework
import paddle.v2.fluid.layers as layers
from paddle.v2.fluid.executor import Executor, g_scope
from paddle.v2.fluid.optimizer import SGDOptimizer

word_dict, verb_dict, label_dict = conll05.get_dict()
word_dict_len = len(word_dict)
label_dict_len = len(label_dict)
pred_len = len(verb_dict)

mark_dict_len = 2
word_dim = 32
mark_dim = 5
hidden_dim = 512
depth = 8
mix_hidden_lr = 1e-3

IS_SPARSE = True
PASS_NUM = 10
BATCH_SIZE = 20

embedding_name = 'emb'


def load_parameter(file_name, h, w):
    with open(file_name, 'rb') as f:
        f.read(16)  # skip header.
        return np.fromfile(f, dtype=np.float32).reshape(h, w)


def db_lstm():
    # 8 features
    word = layers.data(name='word_data', shape=[1], data_type='int64')
    predicate = layers.data(name='verb_data', shape=[1], data_type='int64')
    ctx_n2 = layers.data(name='ctx_n2_data', shape=[1], data_type='int64')
    ctx_n1 = layers.data(name='ctx_n1_data', shape=[1], data_type='int64')
    ctx_0 = layers.data(name='ctx_0_data', shape=[1], data_type='int64')
    ctx_p1 = layers.data(name='ctx_p1_data', shape=[1], data_type='int64')
    ctx_p2 = layers.data(name='ctx_p2_data', shape=[1], data_type='int64')
    mark = layers.data(name='mark_data', shape=[1], data_type='int64')

    predicate_embedding = layers.embedding(
        input=predicate,
        size=[pred_len, word_dim],
        data_type='float32',
        is_sparse=IS_SPARSE,
        param_attr={'name': 'vemb'})

    mark_embedding = layers.embedding(
        input=mark,
        size=[mark_dict_len, mark_dim],
        data_type='float32',
        is_sparse=IS_SPARSE)

    word_input = [word, ctx_n2, ctx_n1, ctx_0, ctx_p1, ctx_p2]
    emb_layers = [
        layers.embedding(
            size=[word_dict_len, word_dim],
            input=x,
            param_attr={'name': embedding_name,
                        'trainable': False}) for x in word_input
    ]
    emb_layers.append(predicate_embedding)
    emb_layers.append(mark_embedding)

    hidden_0_layers = [
        layers.fc(input=emb, size=hidden_dim) for emb in emb_layers
    ]

    hidden_0 = layers.sums(input=hidden_0_layers)

    lstm_0 = layers.dynamic_lstm(
        input=hidden_0,
        size=hidden_dim,
        candidate_activation='relu',
        gate_activation='sigmoid',
        cell_activation='sigmoid')

    # stack L-LSTM and R-LSTM with direct edges
    input_tmp = [hidden_0, lstm_0]

    for i in range(1, depth):
        mix_hidden = layers.sums(input=[
            layers.fc(input=input_tmp[0], size=hidden_dim),
            layers.fc(input=input_tmp[1], size=hidden_dim)
        ])

        lstm = layers.dynamic_lstm(
            input=mix_hidden,
            size=hidden_dim,
            candidate_activation='relu',
            gate_activation='sigmoid',
            cell_activation='sigmoid',
            is_reverse=((i % 2) == 1))

        input_tmp = [mix_hidden, lstm]

    feature_out = layers.sums(input=[
        layers.fc(input=input_tmp[0], size=label_dict_len),
        layers.fc(input=input_tmp[1], size=label_dict_len)
    ])

    return feature_out


def to_lodtensor(data, place):
    seq_lens = [len(seq) for seq in data]
    cur_len = 0
    lod = [cur_len]
    for l in seq_lens:
        cur_len += l
        lod.append(cur_len)
    flattened_data = np.concatenate(data, axis=0).astype("int64")
    flattened_data = flattened_data.reshape([len(flattened_data), 1])
    res = core.LoDTensor()
    res.set(flattened_data, place)
    res.set_lod([lod])
    return res


def main():
    # define network topology
    feature_out = db_lstm()
    target = layers.data(name='target', shape=[1], data_type='int64')
    crf_cost = layers.linear_chain_crf(
        input=feature_out,
        label=target,
        param_attr={"name": 'crfw',
                    "learning_rate": mix_hidden_lr})
    avg_cost = layers.mean(x=crf_cost)
    # TODO(qiao)
    #   1. add crf_decode_layer and evaluator
    #   2. use other optimizer and check why out will be NAN
    sgd_optimizer = SGDOptimizer(learning_rate=0.0001)
    opts = sgd_optimizer.minimize(avg_cost)

    train_data = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.conll05.test(), buf_size=8192),
        batch_size=BATCH_SIZE)
    place = core.CPUPlace()
    exe = Executor(place)

    exe.run(framework.default_startup_program())

    embedding_param = g_scope.find_var(embedding_name).get_tensor()
    embedding_param.set(
        load_parameter(conll05.get_embedding(), word_dict_len, word_dim), place)

    batch_id = 0
    for pass_id in xrange(PASS_NUM):
        for data in train_data():
            word_data = to_lodtensor(map(lambda x: x[0], data), place)
            ctx_n2_data = to_lodtensor(map(lambda x: x[1], data), place)
            ctx_n1_data = to_lodtensor(map(lambda x: x[2], data), place)
            ctx_0_data = to_lodtensor(map(lambda x: x[3], data), place)
            ctx_p1_data = to_lodtensor(map(lambda x: x[4], data), place)
            ctx_p2_data = to_lodtensor(map(lambda x: x[5], data), place)
            verb_data = to_lodtensor(map(lambda x: x[6], data), place)
            mark_data = to_lodtensor(map(lambda x: x[7], data), place)
            target = to_lodtensor(map(lambda x: x[8], data), place)

            outs = exe.run(framework.default_main_program(),
                           feed={
                               'word_data': word_data,
                               'ctx_n2_data': ctx_n2_data,
                               'ctx_n1_data': ctx_n1_data,
                               'ctx_0_data': ctx_0_data,
                               'ctx_p1_data': ctx_p1_data,
                               'ctx_p2_data': ctx_p2_data,
                               'verb_data': verb_data,
                               'mark_data': mark_data,
                               'target': target
                           },
                           fetch_list=[avg_cost])
            avg_cost_val = np.array(outs[0])

            if batch_id % 10 == 0:
                print("avg_cost=" + str(avg_cost_val))

            # exit early for CI
            exit(0)

            batch_id = batch_id + 1


if __name__ == '__main__':
    main()
