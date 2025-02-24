# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import six
import os
import numpy as np
import paddle
from multiprocessing import cpu_count
from paddlenlp.transformers import AutoTokenizer


class InferBackend(object):
    def __init__(self,
                 model_path,
                 batch_size=32,
                 device='cpu',
                 use_fp16=False,
                 enable_quantize=False,
                 set_dynamic_shape=False,
                 num_threads=10):
        int8_model = self.paddle_quantize_model(model_path)
        print(">>> [InferBackend] Creating Engine ...")
        if device == 'gpu' and int8_model or use_fp16:
            from paddle import inference
            self.predictor_type = "inference"
            config = paddle.inference.Config(model_path + ".pdmodel",
                                             model_path + ".pdiparams")
            config.enable_use_gpu(100, 0)
            paddle.set_device("gpu")
            if int8_model and use_fp16:
                print(
                    ">>> [InferBackend] load a paddle quantize model, use_fp16 has been closed..."
                )
                use_fp16 = False

            if use_fp16:
                assert device == 'gpu', "When use_fp16, please set device to gpu and install requirements_gpu.txt."
                config.enable_tensorrt_engine(
                    workspace_size=1 << 30,
                    precision_mode=inference.PrecisionType.Half,
                    max_batch_size=batch_size,
                    min_subgraph_size=5,
                    use_static=False,
                    use_calib_mode=False)
            else:
                config.enable_tensorrt_engine(
                    workspace_size=1 << 30,
                    precision_mode=inference.PrecisionType.Int8,
                    max_batch_size=batch_size,
                    min_subgraph_size=5,
                    use_static=False,
                    use_calib_mode=False)
            shape_file = "shape_info.txt"
            if set_dynamic_shape:
                config.collect_shape_range_info(shape_file)
            else:
                config.enable_tuned_tensorrt_dynamic_shape(shape_file, True)
            config.delete_pass("embedding_eltwise_layernorm_fuse_pass")
            self.predictor = paddle.inference.create_predictor(config)
            self.input_names = [
                name for name in self.predictor.get_input_names()
            ]
            self.input_handles = [
                self.predictor.get_input_handle(name)
                for name in self.predictor.get_input_names()
            ]
            self.output_handles = [
                self.predictor.get_output_handle(name)
                for name in self.predictor.get_output_names()
            ]
        else:
            import paddle2onnx
            import onnxruntime as ort
            import copy
            self.predictor_type = "onnxruntime"
            onnx_model = paddle2onnx.command.c_paddle_to_onnx(
                model_file=model_path + ".pdmodel",
                params_file=model_path + ".pdiparams",
                opset_version=13,
                enable_onnx_checker=True)
            dynamic_quantize_model = onnx_model
            providers = ['CUDAExecutionProvider']
            if enable_quantize:
                float_onnx_file = "model.onnx"
                with open(float_onnx_file, "wb") as f:
                    f.write(onnx_model)
                dynamic_quantize_model = "dynamic_quantize_model.onnx"
                self.dynamic_quantize(float_onnx_file, dynamic_quantize_model)
                providers = ['CPUExecutionProvider']
            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = num_threads
            sess_options.inter_op_num_threads = num_threads
            self.predictor = ort.InferenceSession(
                dynamic_quantize_model,
                sess_options=sess_options,
                providers=providers)
            input_name1 = self.predictor.get_inputs()[0].name
            input_name2 = self.predictor.get_inputs()[1].name
            self.input_handles = [input_name1, input_name2]
            self.output_handles = []
        print(">>> [InferBackend] Engine Created ...")

    def dynamic_quantize(self, input_float_model, dynamic_quantized_model):
        from onnxruntime.quantization import QuantizationMode, quantize_dynamic
        quantize_dynamic(input_float_model, dynamic_quantized_model)

    def paddle_quantize_model(self, model_path):
        model = paddle.jit.load(model_path)
        program = model.program()
        for block in program.blocks:
            for i, op in enumerate(block.ops):
                if op.type.count("quantize"):
                    return True
        return False

    def infer(self, input_dict: dict):
        if self.predictor_type == "inference":
            for idx, input_name in enumerate(self.input_names):
                self.input_handles[idx].copy_from_cpu(input_dict[input_name])
            self.predictor.run()
            output = [
                output_handle.copy_to_cpu()
                for output_handle in self.output_handles
            ]
            return output

        result = self.predictor.run(None, input_dict)
        return result


def token_cls_print_ret(infer_result, input_datas):
    rets = infer_result["value"]
    for i, ret in enumerate(rets):
        print("input data:", input_datas[i])
        print("The model detects all entities:")
        for iterm in ret:
            print("entity:", iterm["entity"], "  label:", iterm["label"],
                  "  pos:", iterm["pos"])
        print("-----------------------------")


class ErniePredictor(object):
    def __init__(self, args):
        if not isinstance(args.device, six.string_types):
            print(
                ">>> [InferBackend] The type of device must be string, but the type you set is: ",
                type(device))
            exit(0)
        args.device = args.device.lower()
        if args.device not in ['cpu', 'gpu']:
            print(
                ">>> [InferBackend] The device must be cpu or gpu, but your device is set to:",
                type(args.device))
            exit(0)

        self.task_name = args.task_name
        self.tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
        if args.task_name == 'seq_cls':
            self.label_names = []
            self.preprocess = self.seq_cls_preprocess
            self.postprocess = self.seq_cls_postprocess
        elif args.task_name == 'token_cls':
            self.label_names = [
                'O', 'B-PER', 'I-PER', 'B-ORG', 'I-ORG', 'B-LOC', 'I-LOC'
            ]
            self.preprocess = self.token_cls_preprocess
            self.postprocess = self.token_cls_postprocess
        else:
            print(
                "[ErniePredictor]: task_name only support seq_cls and token_cls now."
            )
            exit(0)

        self.max_seq_length = args.max_seq_length

        if args.device == 'cpu':
            args.use_fp16 = False
            args.set_dynamic_shape = False
            args.batch_size = 32
        if args.device == 'gpu':
            args.num_threads = cpu_count()
            args.enable_quantize = False
        self.inference_backend = InferBackend(
            args.model_path,
            batch_size=args.batch_size,
            device=args.device,
            use_fp16=args.use_fp16,
            enable_quantize=args.enable_quantize,
            set_dynamic_shape=args.set_dynamic_shape,
            num_threads=args.num_threads)
        if args.set_dynamic_shape:
            # If set_dynamic_shape is turned on, all required dynamic shapes will be automatically set according to the batch_size and max_seq_length.
            self.set_dynamic_shape(args.max_seq_length, args.batch_size)
            exit(0)

    def seq_cls_preprocess(self, input_data: list):
        data = input_data
        # tokenizer + pad
        data = self.tokenizer(
            data, max_length=self.max_seq_length, padding=True, truncation=True)
        input_ids = data["input_ids"]
        token_type_ids = data["token_type_ids"]
        return {
            "input_ids": np.array(
                input_ids, dtype="int64"),
            "token_type_ids": np.array(
                token_type_ids, dtype="int64")
        }

    def seq_cls_postprocess(self, infer_data, input_data):
        infer_data = np.array(infer_data)
        out_dict = {
            "label": infer_data.argmax(axis=-1),
            "confidence": infer_data.max(axis=-1)
        }
        return out_dict

    def token_cls_preprocess(self, data: list):
        # tokenizer + pad
        is_split_into_words = False
        if isinstance(data[0], list):
            is_split_into_words = True
        data = self.tokenizer(
            data,
            max_length=self.max_seq_length,
            padding=True,
            truncation=True,
            is_split_into_words=is_split_into_words)

        input_ids = data["input_ids"]
        token_type_ids = data["token_type_ids"]
        return {
            "input_ids": np.array(
                input_ids, dtype="int64"),
            "token_type_ids": np.array(
                token_type_ids, dtype="int64")
        }

    def token_cls_postprocess(self, infer_data, input_data):
        result = np.array(infer_data[0])
        tokens_label = result.argmax(axis=-1).tolist()
        # 获取batch中每个token的实体
        value = []
        for batch, token_label in enumerate(tokens_label):
            start = -1
            label_name = ""
            items = []
            for i, label in enumerate(token_label):
                if label == 0 and start >= 0:
                    entity = input_data[batch][start:i - 1]
                    if isinstance(entity, list):
                        entity = "".join(entity)
                    items.append({
                        "pos": [start, i - 2],
                        "entity": entity,
                        "label": label_name,
                    })
                    start = -1
                elif label in [1, 3, 5]:
                    start = i - 1
                    label_name = self.label_names[label][2:]
            if start >= 0:
                items.append({
                    "pos": [start, len(token_label) - 1],
                    "entity": input_data[batch][start:len(token_label) - 1],
                    "entity": ""
                })
            value.append(items)

        out_dict = {"value": value, "tokens_label": tokens_label}
        return out_dict

    def set_dynamic_shape(self, max_seq_length, batch_size):
        min_batch_size, max_batch_size, opt_batch_size = 1, batch_size, batch_size
        min_seq_len, max_seq_len, opt_seq_len = 2, args.max_seq_length, 32
        batches = [
            [
                np.zeros(
                    [min_batch_size, min_seq_len], dtype="int64"), np.zeros(
                        [min_batch_size, min_seq_len], dtype="int64")
            ],
            [
                np.zeros(
                    [max_batch_size, max_seq_len], dtype="int64"), np.zeros(
                        [max_batch_size, max_seq_len], dtype="int64")
            ],
            [
                np.zeros(
                    [opt_batch_size, opt_seq_len], dtype="int64"), np.zeros(
                        [opt_batch_size, opt_seq_len], dtype="int64")
            ],
        ]
        for batch in batches:
            self.inference_backend.infer(batch)
        print(
            "[InferBackend] Set dynamic shape finished, please close set_dynamic_shape and restart."
        )

    def infer(self, data):
        return self.inference_backend.infer(data)

    def predict(self, input_data: list):
        preprocess_result = self.preprocess(input_data)
        infer_result = self.infer(preprocess_result)
        result = self.postprocess(infer_result, input_data)

        return result
