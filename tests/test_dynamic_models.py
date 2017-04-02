"""Run trial run on DynamicBot with the TestData Dataset."""
import logging
import sys
import time
import unittest

import numpy as np
import tensorflow as tf
from pydoc import locate

sys.path.append("..")
from chatbot import DynamicBot, ChatBot, SimpleBot
from data import Cornell, Ubuntu, WMT, Reddit, TestData
from chatbot.components import bot_ops
from utils import io_utils
from utils import bot_freezer

test_flags = tf.app.flags
test_flags.DEFINE_string("config", "configs/test_config.yml", "path to config (.yml) file.")
test_flags.DEFINE_string("model", "{}", "Options: chatbot.{DynamicBot,Simplebot,ChatBot}.")
test_flags.DEFINE_string("model_params", "{}", "")
test_flags.DEFINE_string("dataset", "{}", "Options: data.{Cornell,Ubuntu,WMT}.")
test_flags.DEFINE_string("dataset_params", "{}", "")
TEST_FLAGS = test_flags.FLAGS

def _sparse_to_dense(sampled_logits, labels, sampled, num_sampled):
    acc_hits = tf.nn.compute_accidental_hits(labels, sampled, num_true=1)
    acc_indices, acc_ids, acc_weights = acc_hits
    # This is how SparseToDense expects the indices.
    acc_indices_2d = tf.reshape(acc_indices, [-1, 1])
    acc_ids_2d_int32 = tf.reshape(tf.cast(acc_ids, tf.int32), [-1, 1])
    sparse_indices = tf.concat([acc_indices_2d, acc_ids_2d_int32], 1, "sparse_indices")
    # Create sampled_logits_shape = [batch_size, num_sampled]
    sampled_logits_shape = tf.concat([tf.shape(labels)[:1], tf.expand_dims(num_sampled, 0)], 0)
    if sampled_logits.dtype != acc_weights.dtype:
        acc_weights = tf.cast(acc_weights, sampled_logits.dtype)
    return tf.sparse_to_dense(sparse_indices, sampled_logits_shape, acc_weights,
                              default_value=0.0,validate_indices=False)


def get_default_bot(flags=TEST_FLAGS):
    """Creates and returns a fresh bot. Nice for testing specific methods quickly."""
    tf.reset_default_graph()
    config = io_utils.parse_config(flags)
    print("Setting up %s dataset." % config['dataset'])
    dataset = locate(config['dataset'])(config['dataset_params'])
    print("Creating", config['model'], ". . . ")
    bot = locate(config['model'])(dataset, config['model_params'])
    return bot


class TestDynamicModels(unittest.TestCase):

    def setUp(self):
        logging.basicConfig(level=logging.INFO)
        self.log = logging.getLogger('TestDynamicModelsLogger')

    def test_init(self):
        """Basic check that bot creation maintains bug-free."""

        config = io_utils.parse_config(TEST_FLAGS)
        print("Setting up %s dataset." % config['dataset'])
        dataset = locate(config['dataset'])(config['dataset_params'])
        print("Creating", config['model'], ". . . ")
        bot = locate(config['model'])(dataset, config['model_params'])

    def test_train(self):
        flags = TEST_FLAGS
        flags.model_params = "{ckpt_dir: out/test_data, " \
                             "reset_model: True, " \
                             "steps_per_ckpt: 10}"
        bot = get_default_bot(flags)
        #bot.train()

    def test_manual_freeze(self):
        """Make sure we can freeze the bot, unfreeze, and still chat."""

        # ================================================
        # 1. Create & train bot.
        # ================================================
        flags = TEST_FLAGS
        flags.model_params = "{ckpt_dir: out/test_data, " \
                             "reset_model: True, " \
                             "steps_per_ckpt: 10}"
        bot = get_default_bot(flags)
        # Simulate small train sesh on bot.
        self._quick_train(bot)

        # ================================================
        # 2. Recreate a chattable bot.
        # ================================================
        # Recreate bot from scratch with decode set to true.
        self.log.info("Resetting default graph . . . ")
        tf.reset_default_graph()
        flags.model_params = "{ckpt_dir: out/test_data, " \
                             "reset_model: False, " \
                             "decode: True," \
                             "steps_per_ckpt: 10}"
        bot = get_default_bot(flags)
        self.assertTrue(bot.is_chatting)
        self.assertTrue(bot.decode)

        print("Testing quick chat sesh . . . ")
        config = io_utils.parse_config(flags)
        dataset         = locate(config['dataset'])(config['dataset_params'])
        user_input      = io_utils.get_sentence()
        encoder_inputs  = io_utils.sentence_to_token_ids(
            tf.compat.as_bytes(user_input),
            dataset.word_to_idx
        )
        encoder_inputs = np.array([encoder_inputs[::-1]])
        bot.pipeline._feed_dict = {
            bot.pipeline.user_input: encoder_inputs
        }

        # Get output sentence from the chatbot.
        _, _, response = bot.step(forward_only=True)
        # response has shape [1, response_length] and it's last elemeot is EOS_ID. :)
        print("Robot:", dataset.as_words(response[0][:-1]))


        # ================================================
        # 3. Freeze the chattable bot.
        # ================================================
        self.log.info("Calling bot.freeze() . . . ")
        bot.freeze()

        # ================================================
        # 4. Try to unfreeze and use it.
        # ================================================
        self.log.info("Resetting default graph . . . ")
        tf.reset_default_graph()
        self.log.info("Importing frozen graph into default . . . ")
        frozen_graph = bot_freezer.load_graph(bot.ckpt_dir)
        self._print_op_names(frozen_graph)

        self.log.info("Extracting input/output tensors.")
        tensors = bot_freezer.unfreeze_bot(bot.ckpt_dir)
        keys = ['user_input', 'encoder_inputs', 'outputs']
        for k in keys:
            self.assertIsNotNone(tensors[k])


        with tf.Session(graph=frozen_graph) as sess:
            raw_input = io_utils.get_sentence()
            encoder_inputs  = io_utils.sentence_to_token_ids(
            tf.compat.as_bytes(raw_input),
            dataset.word_to_idx
            )
            encoder_inputs = np.array([encoder_inputs[::-1]])
            feed_dict = {tensors['user_input'].name: encoder_inputs}
            plz = sess.run(tensors['outputs'], feed_dict=feed_dict)
            print('plz:', plz)



    def _print_op_names(self, g):
        print("List of Graph Ops:")
        for op in g.get_operations():
            print(op.name)

    def _quick_train(self, bot):
        """Quickly train manually on some test data."""
        coord   = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=bot.sess, coord=coord)
        for _ in range(10):
            bot.step()
        summaries, loss, _ = bot.step()
        bot.save(summaries=summaries)
        coord.request_stop()
        coord.join(threads)


    def test_chat(self):
        """Feed the training sentences to the bot during conversation.
        It should respond somewhat predictably on these for now.
        """

        data_dir = '/home/brandon/terabyte/Datasets/test_data'
        dataset = TestData(data_dir)
        dataset.convert_to_tf_records('train')
        dataset.convert_to_tf_records('valid')

        print("Should I train first?")
        should_train = io_utils.get_sentence()
        is_chatting = False if should_train == 'y' else True
        print("is chatting is ", is_chatting)

        state_size = 2048
        embed_size = 64
        num_layers = 3
        learning_rate = 0.1
        dropout_prob = 0.5
        ckpt_dir = 'out/st_%d_nl_%d_emb_%d_lr_%d_drop_5' % (
            state_size, num_layers, embed_size, int(100 * learning_rate)
        )

        bot = DynamicBot(dataset,
                         ckpt_dir=ckpt_dir,
                         batch_size=4,
                         learning_rate=learning_rate,
                         state_size=state_size,
                         embed_size=embed_size,
                         num_layers=num_layers,
                         dropout_prob=dropout_prob,
                         is_chatting=is_chatting)
        bot.compile(reset=(not is_chatting))
        if not is_chatting:
            bot.train(dataset)
        else:
            sentence_generator = dataset.sentence_generator()
            try:
                while True:
                    sentence = next(sentence_generator)
                    print("Human:\t", sentence)
                    print("Bot:  \t", bot(sentence))
                    print()
                    time.sleep(1)
            except (KeyboardInterrupt, StopIteration):
                print('Bleep bloop. Goodbye.')

    def test_target_weights(self):
        """Make sure target weights set PAD targets to zero."""
        data_dir = '/home/brandon/terabyte/Datasets/test_data'
        dataset = TestData(data_dir)

        is_chatting = False
        state_size = 256
        embed_size = 64
        num_layers = 3
        learning_rate = 0.1
        dropout_prob = 0.5
        ckpt_dir = 'out/test_target_weights'

        bot = DynamicBot(dataset,
                         ckpt_dir=ckpt_dir,
                         batch_size=4,
                         learning_rate=learning_rate,
                         state_size=state_size,
                         embed_size=embed_size,
                         num_layers=num_layers,
                         dropout_prob=dropout_prob,
                         is_chatting=is_chatting)

        # Test the following two lines used by DynamicBot.compile().
        target_labels = bot.decoder_inputs[:, 1:]
        target_weights = tf.cast(target_labels > 0, target_labels.dtype)
        super(DynamicBot, bot).compile(reset=True)
        with bot.sess as sess:
            answer = 'n'
            while answer == 'n':

                coord   = tf.train.Coordinator()
                threads = tf.train.start_queue_runners(sess=sess, coord=coord)
                inp, weights = sess.run([target_labels, target_weights])
                print("\ndec inp:")
                print(inp)
                print("target weights:")
                print(weights)

                print("Are you satisfied? [y/n]")
                answer = io_utils.get_sentence()
                if answer == 'y':
                    coord.request_stop()
                    coord.join(threads)
                    bot.close()

    def test_sampled_chat(self):
        """Same as test_chat but trains on new custom dynamic sampled softmax loss."""

        data_dir = '/home/brandon/terabyte/Datasets/test_data'
        dataset = TestData(data_dir)
        dataset.convert_to_tf_records('train')
        dataset.convert_to_tf_records('valid')

        print("Should I train first?")
        should_train = io_utils.get_sentence()
        is_chatting = False if should_train == 'y' else True
        print("is chatting is ", is_chatting)

        state_size = 256
        embed_size = 64
        num_layers = 3
        learning_rate = 0.1
        dropout_prob = 0.5
        ckpt_dir = 'out/sampled_st_%d_nl_%d_emb_%d_lr_%d_drop_5' % (
            state_size, num_layers, embed_size, int(100 * learning_rate)
        )

        num_samples = 40
        bot = DynamicBot(dataset,
                         num_samples=num_samples,
                         ckpt_dir=ckpt_dir,
                         batch_size=4,
                         learning_rate=learning_rate,
                         state_size=state_size,
                         embed_size=embed_size,
                         num_layers=num_layers,
                         dropout_prob=dropout_prob,
                         is_chatting=is_chatting)
        bot.compile(reset=(not is_chatting), sampled_loss=True)
        if not is_chatting:
            print("ENTERING TRAINING")
            bot.train(dataset)
        else:
            sentence_generator = dataset.sentence_generator()
            try:
                while True:
                    sentence = next(sentence_generator)
                    print("Human:\t", sentence)
                    print("Bot:  \t", bot(sentence))
                    print()
                    time.sleep(1)
            except (KeyboardInterrupt, StopIteration):
                print('Bleep bloop. Goodbye.')


    def test_sampled_bot(self):
        data_dir = '/home/brandon/terabyte/Datasets/cornell'
        dataset = Cornell(data_dir, 40000)
        dataset.convert_to_tf_records('train')
        dataset.convert_to_tf_records('valid')

        is_chatting = False
        state_size = 128
        embed_size = state_size
        num_layers = 3
        learning_rate = 0.1
        dropout_prob = 0.5
        ckpt_dir = 'out'

        bot = DynamicBot(dataset,
                         ckpt_dir=ckpt_dir,
                         batch_size=32,
                         steps_per_ckpt=10,
                         learning_rate=learning_rate,
                         state_size=state_size,
                         embed_size=embed_size,
                         num_layers=num_layers,
                         dropout_prob=dropout_prob,
                         is_chatting=is_chatting)
        print('compiling')
        bot.compile(reset=(not is_chatting))
        bot.train(dataset)

    def test_sampled_softmax_from_scratch(self):
        """Comparing behavior of new dynamic_sampled_softmax_loss with a completely
        transparent version 'from scratch'. Why? Because there doesn't seem any way
        to incorporate target-weights attached to padded inputs while also using tensorflow's
        sampled_softmax_loss, as opposed to the much cleaner tf.loss.sparse_softmax_cross_entropy.

        Goal: Construct a sampling loss function that can accept the following tensors:
            1. Outputs. [batch_size, None, state_size] Floats.
            2. Labels.  [batch_size, None]. Integers.
            3. Weights. [batch_size, None].

            Constraints:
                DynamicBot.compile must pass in these arguments such that
                    tf.shape(outputs[:, :, i]) == tf.shape(labels) for all/arbitrary i.
                    tf.shape(labels) == tf.shape(weights).
        """



        # Test it works:
        seq_len = 20
        batch_size = 64
        state_size = 128
        num_samples = 10
        vocab_size = 2000
        w = tf.get_variable("w", [state_size, vocab_size], dtype=tf.float32)
        b = tf.get_variable("b", [vocab_size], dtype=tf.float32)
        output_projection = (w, b)
        labels = np.random.randint(0, vocab_size, size=(batch_size, seq_len))
        state_outputs = np.random.random(size=(batch_size, seq_len, state_size))
        state_outputs=tf.cast(state_outputs, tf.float32)

        print("\nExpected quantities:")
        print("\tbatch_times_none:", batch_size * seq_len)
        print("\tstate_size:", state_size)
        print("\tshape(state_outputs):", (batch_size, seq_len, state_size))


        with tf.Session() as sess:
            sess.run(tf.global_variables_initializer())
            print('\n=========== FROM SCRATCH ============')
            loss = bot_ops.dynamic_sampled_softmax_loss(labels=labels,
                                                        logits=state_outputs,
                                                        output_projection=output_projection,
                                                        vocab_size=vocab_size,
                                                        from_scratch=True,
                                                        name="map_version",
                                                        num_samples=num_samples)
            loss = sess.run(loss)
            print('loss:\n', loss)

            print('\n=========== MAP VERSION ============')
            loss = bot_ops.dynamic_sampled_softmax_loss(labels=labels,
                                                        logits=state_outputs,
                                                        output_projection=output_projection,
                                                        vocab_size=vocab_size,
                                                        from_scratch=False,
                                                        name="from_scratch",
                                                        num_samples=num_samples)

            loss = sess.run(loss)
            print('loss:\n', loss)
            time_major_outputs = tf.reshape(state_outputs, [seq_len, batch_size, state_size])
            # Project batch at single timestep from state space to output space.
            def proj_op(bo): return tf.matmul(bo, w) + b
            # Get projected output states; 3D Tensor with shape [batch_size, seq_len, ouput_size].
            projected_state = tf.map_fn(proj_op, time_major_outputs)
            proj_out = tf.reshape(projected_state, [batch_size, seq_len, vocab_size])

            print('\n=========== ACTUAL ============')
            loss = tf.losses.sparse_softmax_cross_entropy(
                labels=labels, logits=proj_out)
            loss = sess.run(loss)
            print('loss:\n', loss)

