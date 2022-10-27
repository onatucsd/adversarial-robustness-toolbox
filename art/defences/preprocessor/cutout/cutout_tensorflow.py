# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2022
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements the Cutout data augmentation defence in TensorFlow.

| Paper link: https://arxiv.org/abs/1708.04552

| Please keep in mind the limitations of defences. For more information on the limitations of this defence,
    see https://arxiv.org/abs/1803.09868 . For details on how to evaluate classifier security in general, see
    https://arxiv.org/abs/1902.06705
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import logging
from typing import Optional, Tuple, TYPE_CHECKING

from tqdm.auto import trange

from art.defences.preprocessor.preprocessor import PreprocessorTensorFlowV2

if TYPE_CHECKING:
    # pylint: disable=C0412
    import tensorflow as tf

logger = logging.getLogger(__name__)


class CutoutTensorFlowV2(PreprocessorTensorFlowV2):
    """
    Implement the Cutout data augmentation defence approach in TensorFlow v2.

    | Paper link: https://arxiv.org/abs/1708.04552

    | Please keep in mind the limitations of defences. For more information on the limitations of this defence,
        see https://arxiv.org/abs/1803.09868 . For details on how to evaluate classifier security in general, see
        https://arxiv.org/abs/1902.06705
    """

    params = ["length", "channels_first", "verbose"]

    def __init__(
        self,
        length: int,
        channels_first: bool = False,
        apply_fit: bool = False,
        apply_predict: bool = True,
        verbose: bool = False,
    ):
        """
        Create an instance of a Cutout data augmentation object.

        :param length: Maximum length of the bounding box.
        :param channels_first: Set channels first or last.
        :param apply_fit: True if applied during fitting/training.
        :param apply_predict: True if applied during predicting.
        :param verbose: Show progress bars.
        """
        super().__init__(is_fitted=True, apply_fit=apply_fit, apply_predict=apply_predict)
        self.length = length
        self.channels_first = channels_first
        self.verbose = verbose
        self._check_params()

    def forward(self, x: "tf.Tensor", y: Optional["tf.Tensor"] = None) -> Tuple["tf.Tensor", Optional["tf.Tensor"]]:
        """
        Apply Cutout data augmentation to sample `x`.

        :param x: Sample to compress with shape of `NCHW` or `NHWC`. The `x` values are expected to be in
                  the data range [0, 1] or [0, 255].
        :param y: Labels of the sample `x`. This function does not affect them in any way.
        :return: Data augmented sample.
        """
        import tensorflow as tf  # lgtm [py/repeated-import]

        x_ndim = len(x.shape)

        if x_ndim == 4:
            if self.channels_first:
                # NCHW
                n, _, height, width = x.shape
            else:
                # NHWC
                n, height, width, _ = x.shape
        else:
            raise ValueError("Unrecognized input dimension. Cutout can only be applied to image data.")

        masks = tf.Variable(tf.ones_like(x), trainable=False)

        # generate a random bounding box per image
        for idx in trange(n, desc="Cutout", disable=not self.verbose):
            # uniform sampling
            center_y = tf.random.uniform(shape=[], maxval=height, dtype=tf.int32)  # pylint: disable=E1123
            center_x = tf.random.uniform(shape=[], maxval=width, dtype=tf.int32)  # pylint: disable=E1123
            bby1 = tf.clip_by_value(center_y - self.length // 2, 0, height)
            bbx1 = tf.clip_by_value(center_x - self.length // 2, 0, width)
            bby2 = tf.clip_by_value(center_y + self.length // 2, 0, height)
            bbx2 = tf.clip_by_value(center_x + self.length // 2, 0, width)

            # zero out the bounding box
            if self.channels_first:
                bbox = masks[idx, :, bbx1:bbx2, bby1:bby2]
            else:
                bbox = masks[idx, bbx1:bbx2, bby1:bby2, :]
            bbox.assign(tf.zeros_like(bbox))

        x_aug = x * masks

        return x_aug, y

    def _check_params(self) -> None:
        if self.length <= 0:
            raise ValueError("Bounding box length must be positive.")
