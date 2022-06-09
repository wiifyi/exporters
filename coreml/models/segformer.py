# Copyright 2022 The HuggingFace Team. All rights reserved.
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
"""Core ML conversion for SegFormer."""

import json
import numpy as np
import torch
from torch import nn

import coremltools as ct
from coremltools.models.neural_network import quantization_utils

from transformers import SegformerFeatureExtractor, SegformerModel, SegformerForImageClassification, SegformerForSemanticSegmentation
from ..coreml_utils import *


class Wrapper(torch.nn.Module):
    def __init__(self, model, feature_extractor, do_upsample, do_argmax):
        super().__init__()
        self.model = model.eval()
        self.feature_extractor = feature_extractor
        self.do_upsample = do_upsample
        self.do_argmax = do_argmax

    def forward(self, inputs):
        # Core ML's image preprocessing does not allow a different
        # scaling factor for each color channel, so do this manually.
        image_std = torch.tensor(self.feature_extractor.image_std).reshape(1, -1, 1, 1)
        inputs = inputs / image_std

        outputs = self.model(inputs, return_dict=False)

        if isinstance(self.model, SegformerForImageClassification):
            return nn.functional.softmax(outputs[0], dim=1)

        if isinstance(self.model, SegformerForSemanticSegmentation):
            x = outputs[0]
            if self.do_upsample:
                x = nn.functional.interpolate(x, size=inputs.shape[-2:], mode="bilinear", align_corners=False)
            if self.do_argmax:
                x = x.argmax(1)
            return x

        if isinstance(self.model, SegformerModel):
            return outputs[0]  # last_hidden_state

        return None


def export(
    torch_model,
    feature_extractor: SegformerFeatureExtractor,
    do_upsample: bool = True,
    do_argmax: bool = True,
    quantize: str = "float32",
    legacy: bool = False,
) -> ct.models.MLModel:
    if not isinstance(feature_extractor, SegformerFeatureExtractor):
        raise ValueError(f"Unknown feature extractor: {feature_extractor}")

    wrapper = Wrapper(torch_model, feature_extractor, do_upsample, do_argmax).eval()

    scale = 1.0 / 255
    bias = [
        -feature_extractor.image_mean[0],
        -feature_extractor.image_mean[1],
        -feature_extractor.image_mean[2],
    ]

    image_size = feature_extractor.size
    image_shape = (1, 3, image_size, image_size)
    example_input = torch.rand(image_shape) * 2.0 - 1.0

    traced_model = torch.jit.trace(wrapper, example_input, strict=True)

    convert_kwargs = { }
    if not legacy:
        convert_kwargs["compute_precision"] = ct.precision.FLOAT16 if quantize == "float16" else ct.precision.FLOAT32

    if isinstance(torch_model, SegformerForImageClassification):
        class_labels = [torch_model.config.id2label[x] for x in range(torch_model.config.num_labels)]
        classifier_config = ct.ClassifierConfig(class_labels)
        convert_kwargs['classifier_config'] = classifier_config

    mlmodel = ct.convert(
        traced_model,
        inputs=[ct.ImageType(name="image", shape=image_shape, scale=scale, bias=bias,
                             color_layout="RGB", channel_first=True)],
        convert_to="neuralnetwork" if legacy else "mlprogram",
        **convert_kwargs,
    )

    spec = mlmodel._spec

    user_defined_metadata = {}
    if torch_model.config.transformers_version:
        user_defined_metadata["transformers_version"] = torch_model.config.transformers_version

    if isinstance(torch_model, SegformerForImageClassification):
        probs_output_name = spec.description.predictedProbabilitiesName
        ct.utils.rename_feature(spec, probs_output_name, "probabilities")
        spec.description.predictedProbabilitiesName = "probabilities"

        mlmodel.input_description["image"] = "Image to be classified"
        mlmodel.output_description["probabilities"] = "Probability of each category"
        mlmodel.output_description["classLabel"] = "Category with the highest score"

    if isinstance(torch_model, SegformerForSemanticSegmentation):
        new_output_name = "classLabels" if do_argmax else "probabilities"

        # Rename the segmentation output.
        output = spec.description.output[0]
        ct.utils.rename_feature(spec, output.name, new_output_name)

        # Fill in the shapes for the output tensors.
        with torch.no_grad():
            temp = traced_model(example_input)

        set_multiarray_shape(output, temp.shape)

        mlmodel.input_description["image"] = "Image input"
        mlmodel.output_description[new_output_name] = "Segmentation map"

        labels = get_labels_as_list(torch_model)
        user_defined_metadata["classes"] = ",".join(labels)

        # Make the model available in Xcode's previewer.
        mlmodel.user_defined_metadata["com.apple.coreml.model.preview.type"] = "imageSegmenter"
        mlmodel.user_defined_metadata["com.apple.coreml.model.preview.params"] = json.dumps({"labels": labels})

    if isinstance(torch_model, SegformerModel):
        # Rename the output and fill in its shape.
        output = spec.description.output[0]
        ct.utils.rename_feature(spec, output.name, "last_hidden_state")

        with torch.no_grad():
            temp = traced_model(example_input)

        set_multiarray_shape(get_output_named(spec, "last_hidden_state"), temp.shape)

        mlmodel.input_description["image"] = "Image input"
        mlmodel.output_description["last_hidden_state"] = "Hidden states from the last layer"

    if len(user_defined_metadata) > 0:
        spec.description.metadata.userDefined.update(user_defined_metadata)

    # Reload the model in case any input / output names were changed.
    mlmodel = ct.models.MLModel(mlmodel._spec, weights_dir=mlmodel.weights_dir)

    if legacy and quantize == "float16":
        mlmodel = quantization_utils.quantize_weights(mlmodel, nbits=16)

    return mlmodel