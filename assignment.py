import tensorflow as tf
from tensorflow.keras import Model, Sequential
from tensorflow.keras.layers import (
    Dense,
    Flatten,
    Conv2D,
    BatchNormalization,
    LeakyReLU,
    Reshape,
    Conv2DTranspose,
    Activation,
)
from preprocess import load_image_batch
import tensorflow_gan as tfgan
import tensorflow_hub as hub
from tqdm import tqdm

import numpy as np

from imageio import imwrite
import os
import argparse

# Killing optional CPU driver warnings
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

gpu_available = tf.test.is_gpu_available()
print("GPU Available: ", gpu_available)

## --------------------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="DCGAN")

parser.add_argument(
    "--img-dir",
    type=str,
    default="./data/celebA",
    help="Data where training images live",
)

parser.add_argument(
    "--out-dir",
    type=str,
    default="./output",
    help="Data where sampled output images will be written",
)

parser.add_argument(
    "--mode", type=str, default="train", help='Can be "train" or "test"'
)

parser.add_argument(
    "--restore-checkpoint",
    action="store_true",
    help="Use this flag if you want to resuming training from a previously-saved checkpoint",
)

parser.add_argument(
    "--z-dim", type=int, default=100, help="Dimensionality of the latent space"
)

parser.add_argument(
    "--batch-size",
    type=int,
    default=128,
    help="Sizes of image batches fed through the network",
)

parser.add_argument(
    "--num-data-threads",
    type=int,
    default=2,
    help="Number of threads to use when loading & pre-processing training images",
)

parser.add_argument(
    "--num-epochs",
    type=int,
    default=10,
    help="Number of passes through the training data to make before stopping",
)

parser.add_argument(
    "--learn-rate", type=float, default=0.0002, help="Learning rate for Adam optimizer"
)

parser.add_argument(
    "--beta1", type=float, default=0.5, help='"beta1" parameter for Adam optimizer'
)

parser.add_argument(
    "--num-gen-updates",
    type=int,
    default=2,
    help="Number of generator updates per discriminator update",
)

parser.add_argument(
    "--log-every",
    type=int,
    default=7,
    help="Print losses after every [this many] training iterations",
)

parser.add_argument(
    "--save-every",
    type=int,
    default=500,
    help="Save the state of the network after every [this many] training iterations",
)

parser.add_argument(
    "--device",
    type=str,
    default="GPU:0" if gpu_available else "CPU:0",
    help="specific the device of computation eg. CPU:0, GPU:0, GPU:1, GPU:2, ... ",
)

args = parser.parse_args()

## --------------------------------------------------------------------------------------

# Numerically stable logarithm function
def log(x):
    """
    Finds the stable log of x

    :param x: 
    """
    return tf.math.log(tf.maximum(x, 1e-5))


## --------------------------------------------------------------------------------------

# For evaluating the quality of generated images
# Frechet Inception Distance measures how similar the generated images are to the real ones
# https://nealjean.com/ml/frechet-inception-distance/
# Lower is better
module = tf.keras.Sequential(
    [
        hub.KerasLayer(
            "https://tfhub.dev/google/tf2-preview/inception_v3/classification/4",
            output_shape=[1001],
        )
    ]
)


def fid_function(real_image_batch, generated_image_batch):
    """
    Given a batch of real images and a batch of generated images, this function pulls down a pre-trained inception 
    v3 network and then uses it to extract the activations for both the real and generated images. The distance of 
    these activations is then computed. The distance is a measure of how "realistic" the generated images are.

    :param real_image_batch: a batch of real images from the dataset, shape=[batch_size, height, width, channels]
    :param generated_image_batch: a batch of images generated by the generator network, shape=[batch_size, height, width, channels]

    :return: the inception distance between the real and generated images, scalar
    """
    INCEPTION_IMAGE_SIZE = (299, 299)
    real_resized = tf.image.resize(real_image_batch, INCEPTION_IMAGE_SIZE)
    fake_resized = tf.image.resize(generated_image_batch, INCEPTION_IMAGE_SIZE)
    module.build([None, 299, 299, 3])
    real_features = module(real_resized)
    fake_features = module(fake_resized)
    return tfgan.eval.frechet_classifier_distance_from_activations(
        real_features, fake_features
    )


class Generator_Model(tf.keras.Model):
    def __init__(self):
        """
        The model for the generator network is defined here. 
        """
        super(Generator_Model, self).__init__()
        # TODO: Define the model, loss, and optimizer

        layers = [
            # Project and reshape (Input is bsz * z-dim)
            Dense(4 * 4 * 1024, activation="relu"),
            Reshape([4, 4, 1024]),
            # First Deconv to 8x8x512, filters 4, stride 2
            Conv2DTranspose(
                512,
                4,
                2,
                "SAME",
                activation="relu",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            BatchNormalization(),
            # => 16x16x256
            Conv2DTranspose(
                256,
                4,
                2,
                "SAME",
                activation="relu",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            BatchNormalization(),
            # => 32x32x128
            Conv2DTranspose(
                128,
                4,
                2,
                "SAME",
                activation="relu",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            BatchNormalization(),
            # => 64x64x3
            Conv2DTranspose(
                3,
                4,
                2,
                "SAME",
                activation="tanh",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
        ]

        self.net = Sequential(layers)

    @tf.function
    def call(self, inputs):
        """
        Executes the generator model on the random noise vectors.

        :param inputs: a batch of random noise vectors, shape=[batch_size, z_dim]

        :return: prescaled generated images, shape=[batch_size, height, width, channel]
        """
        # TODO: Call the forward pass
        return self.net(inputs)

    @tf.function
    def loss_function(self, disc_fake_output):
        """
        Outputs the loss given the discriminator output on the generated images.

        :param disc_fake_output: the discrimator output on the generated images, shape=[batch_size,1]

        :return: loss, the cross entropy loss, scalar
        """
        # TODO: Calculate the loss
        return tf.reduce_mean(
            tf.keras.losses.binary_crossentropy(
                tf.ones_like(disc_fake_output), disc_fake_output
            )
        )


class Discriminator_Model(tf.keras.Model):
    def __init__(self):
        super(Discriminator_Model, self).__init__()
        """
        The model for the discriminator network is defined here. 
        """
        # TODO: Define the model, loss, and optimizer
        layers = [
            # Input is bszx64x64x3 => 32x32x128
            Conv2D(
                128,
                4,
                2,
                "SAME",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            LeakyReLU(alpha=0.2),
            # => 16x16x256
            Conv2D(
                256,
                4,
                2,
                "SAME",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            LeakyReLU(alpha=0.2),
            BatchNormalization(),
            # => 8x8x512
            Conv2D(
                512,
                4,
                2,
                "SAME",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            LeakyReLU(alpha=0.2),
            BatchNormalization(),
            # => 4x4x1024
            Conv2D(
                1024,
                4,
                2,
                "SAME",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            LeakyReLU(alpha=0.2),
            BatchNormalization(),
            # Down to one pixel
            Conv2D(
                1,
                4,
                1,
                "VALID",
                kernel_initializer=tf.random_normal_initializer(stddev=0.02),
            ),
            LeakyReLU(alpha=0.2),
            Flatten(),
            Activation("sigmoid"),
        ]

        self.net = Sequential(layers)

    @tf.function
    def call(self, inputs):
        """
        Executes the discriminator model on a batch of input images and outputs whether it is real or fake.

        :param inputs: a batch of images, shape=[batch_size, height, width, channels]

        :return: a batch of values indicating whether the image is real or fake, shape=[batch_size, 1]
        """
        # TODO: Call the forward pass
        return self.net(inputs)

    def loss_function(self, disc_real_output, disc_fake_output):
        """
        Outputs the discriminator loss given the discriminator model output on the real and generated images.

        :param disc_real_output: discriminator output on the real images, shape=[batch_size, 1]
        :param disc_fake_output: discriminator output on the generated images, shape=[batch_size, 1]

        :return: loss, the combined cross entropy loss, scalar
        """
        # TODO: Calculate the loss
        loss = tf.reduce_mean(
            tf.keras.losses.binary_crossentropy(
                tf.zeros_like(disc_fake_output), disc_fake_output
            )
        )
        loss += tf.reduce_mean(
            tf.keras.losses.binary_crossentropy(
                tf.ones_like(disc_fake_output), disc_real_output
            )
        )

        return loss


## --------------------------------------------------------------------------------------


def optimize(
    tape: tf.GradientTape, model: tf.keras.Model, loss: tf.Tensor, optimizer
) -> None:
    """ This optimizes a model with respect to its loss
  
  Inputs:
  - tape: the Gradient Tape
  - model: the model to be trained
  - loss: the model's loss
  """
    # TODO: calculate the gradients our input model and apply them
    gradients = tape.gradient(loss, model.trainable_variables)
    optimizer.apply_gradients(zip(gradients, model.trainable_variables))


def gen_noise():
    return tf.random.uniform([args.batch_size, args.z_dim], minval=1, maxval=1)


# Train the model for one epoch.
def train(generator, discriminator, dataset_iterator, manager):
    """
    Train the model for one epoch. Save a checkpoint every 500 or so batches.

    :param generator: generator model
    :param discriminator: discriminator model
    :param dataset_ierator: iterator over dataset, see preprocess.py for more information
    :param manager: the manager that handles saving checkpoints by calling save()

    :return: The average FID score over the epoch
    """

    pbar = tqdm(dataset_iterator)
    total_fid = 0
    total_fid_n = 0

    # Loop over our data until we run out
    for iteration, batch in enumerate(pbar):
        # TODO: Train the model

        gen_input = gen_noise()

        with tf.GradientTape(persistent=True) as tape:
            gen_output = generator(gen_input)

            logits_fake = discriminator(gen_output)
            logits_real = discriminator(batch)

            g_loss = generator.loss_function(logits_fake)

            if iteration % args.num_gen_updates == 0:
                d_loss = discriminator.loss_function(logits_real, logits_fake)

        pbar.set_description(f" g_loss: {g_loss:1.3f}, d_loss: {d_loss:1.3f}")

        optimizer = tf.keras.optimizers.Adam(
            learning_rate=args.learn_rate, beta_1=args.beta1
        )

        g_gradients = tape.gradient(g_loss, generator.trainable_variables)
        optimizer.apply_gradients(zip(g_gradients, generator.trainable_variables))

        if iteration % args.num_gen_updates == 0:
            d_gradients = tape.gradient(d_loss, discriminator.trainable_variables)
            optimizer.apply_gradients(
                zip(d_gradients, discriminator.trainable_variables)
            )

        # Save
        if iteration % args.save_every == 0:
            manager.save()

        # Calculate inception distance and track the fid in order
        # to return the average
        if iteration % 500 == 0:
            fid_ = fid_function(batch, gen_output)
            total_fid += fid_
            total_fid_n += 1
            print("**** INCEPTION DISTANCE: %g ****" % fid_)

    return total_fid / total_fid_n


# Test the model by generating some samples.
def test(generator):
    """
    Test the model.

    :param generator: generator model

    :return: None
    """
    # TODO: Replace 'None' with code to sample a batch of random images
    img = generator(gen_noise())

    ### Below, we've already provided code to save these generated images to files on disk
    # Rescale the image from (-1, 1) to (0, 255)
    img = ((img / 2) - 0.5) * 255
    # Convert to uint8
    img = img.astype(np.uint8)
    # Save images to disk
    for i in range(0, args.batch_size):
        img_i = img[i]
        s = args.out_dir + "/" + str(i) + ".png"
        imwrite(s, img_i)


## --------------------------------------------------------------------------------------


def main():
    # Load a batch of images (to feed to the discriminator)
    dataset_iterator = load_image_batch(
        args.img_dir, batch_size=args.batch_size, n_threads=args.num_data_threads
    )

    # Initialize generator and discriminator models
    generator = Generator_Model()
    discriminator = Discriminator_Model()

    # For saving/loading models
    checkpoint_dir = "./checkpoints"
    checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt")
    checkpoint = tf.train.Checkpoint(generator=generator, discriminator=discriminator)
    manager = tf.train.CheckpointManager(checkpoint, checkpoint_dir, max_to_keep=3)
    # Ensure the output directory exists
    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)

    if args.restore_checkpoint or args.mode == "test":
        # restores the latest checkpoint using from the manager
        checkpoint.restore(manager.latest_checkpoint)

    try:
        # Specify an invalid GPU device
        with tf.device("/device:" + args.device):
            if args.mode == "train":
                for epoch in range(0, args.num_epochs):
                    print(
                        "========================== EPOCH %d  =========================="
                        % epoch
                    )
                    avg_fid = train(generator, discriminator, dataset_iterator, manager)
                    print("Average FID for Epoch: " + str(avg_fid))
                    # Save at the end of the epoch, too
                    print("**** SAVING CHECKPOINT AT END OF EPOCH ****")
                    manager.save()
            if args.mode == "test":
                test(generator)
    except RuntimeError as e:
        print(e)


if __name__ == "__main__":
    main()

