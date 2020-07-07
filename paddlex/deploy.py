# copyright (c) 2020 PaddlePaddle Authors. All Rights Reserve.
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
import os
import os.path as osp
import cv2
import numpy as np
import yaml
import paddlex
import paddle.fluid as fluid
from paddlex.cv.transforms import build_transforms
from paddlex.cv.models import BaseClassifier, YOLOv3, FasterRCNN, MaskRCNN, DeepLabv3p


class Predictor:
    def __init__(self,
                 model_dir,
                 use_gpu=True,
                 gpu_id=0,
                 use_mkl=False,
                 use_trt=False,
                 use_glog=False,
                 memory_optimize=True):
        """ 创建Paddle Predictor

            Args:
                model_dir: 模型路径（必须是导出的部署或量化模型）
                use_gpu: 是否使用gpu，默认True
                gpu_id: 使用gpu的id，默认0
                use_mkl: 是否使用mkldnn计算库，CPU情况下使用，默认False
                use_trt: 是否使用TensorRT，默认False
                use_glog: 是否启用glog日志, 默认False
                memory_optimize: 是否启动内存优化，默认True
        """
        if not osp.isdir(model_dir):
            raise Exception("[ERROR] Path {} not exist.".format(model_dir))
        if not osp.exists(osp.join(model_dir, "model.yml")):
            raise Exception("There's not model.yml in {}".format(model_dir))
        with open(osp.join(model_dir, "model.yml")) as f:
            self.info = yaml.load(f.read(), Loader=yaml.Loader)

        self.status = self.info['status']

        if self.status != "Quant" and self.status != "Infer":
            raise Exception("[ERROR] Only quantized model or exported "
                            "inference model is supported.")

        self.model_dir = model_dir
        self.model_type = self.info['_Attributes']['model_type']
        self.model_name = self.info['Model']
        self.num_classes = self.info['_Attributes']['num_classes']
        self.labels = self.info['_Attributes']['labels']
        if self.info['Model'] == 'MaskRCNN':
            if self.info['_init_params']['with_fpn']:
                self.mask_head_resolution = 28
            else:
                self.mask_head_resolution = 14
        transforms_mode = self.info.get('TransformsMode', 'RGB')
        if transforms_mode == 'RGB':
            to_rgb = True
        else:
            to_rgb = False
        self.transforms = build_transforms(self.model_type,
                                           self.info['Transforms'], to_rgb)
        self.predictor = self.create_predictor(
            use_gpu, gpu_id, use_mkl, use_trt, use_glog, memory_optimize)

    def create_predictor(self,
                         use_gpu=True,
                         gpu_id=0,
                         use_mkl=False,
                         use_trt=False,
                         use_glog=False,
                         memory_optimize=True):
        config = fluid.core.AnalysisConfig(
            os.path.join(self.model_dir, '__model__'),
            os.path.join(self.model_dir, '__params__'))

        if use_gpu:
            # 设置GPU初始显存(单位M)和Device ID
            config.enable_use_gpu(100, gpu_id)
        else:
            config.disable_gpu()
        if use_mkl:
            config.enable_mkldnn()
        if use_glog:
            config.enable_glog_info()
        else:
            config.disable_glog_info()
        if memory_optimize:
            config.enable_memory_optim()

        # 开启计算图分析优化，包括OP融合等
        config.switch_ir_optim(True)
        # 关闭feed和fetch OP使用，使用ZeroCopy接口必须设置此项
        config.switch_use_feed_fetch_ops(False)
        predictor = fluid.core.create_paddle_predictor(config)
        return predictor

    def preprocess(self, image, thread_num=1):
        """ 对图像做预处理

            Args:
                image(str|np.ndarray): 图像路径；或者是解码后的排列格式为（H, W, C）且类型为float32且为BGR格式的数组。
                    或者是对数（元）组中的图像同时进行预测，数组中的元素可以是图像路径，也可以是解码后的排列格式为（H，W，C）
                    且类型为float32且为BGR格式的数组。
        """
        res = dict()
        if self.model_type == "classifier":
            im = BaseClassifier._preprocess(
                image,
                self.transforms,
                self.model_type,
                self.model_name,
                thread_num=thread_num)
            res['image'] = im
        elif self.model_type == "detector":
            if self.model_name == "YOLOv3":
                im, im_size = YOLOv3._preprocess(
                    image,
                    self.transforms,
                    self.model_type,
                    self.model_name,
                    thread_num=thread_num)
                res['image'] = im
                res['im_size'] = im_size
            if self.model_name.count('RCNN') > 0:
                im, im_resize_info, im_shape = FasterRCNN._preprocess(
                    image,
                    self.transforms,
                    self.model_type,
                    self.model_name,
                    thread_num=thread_num)
                res['image'] = im
                res['im_info'] = im_resize_info
                res['im_shape'] = im_shape
        elif self.model_type == "segmenter":
            im, im_imfo = DeepLabv3p._preprocess(
                image,
                self.transforms,
                self.model_type,
                self.model_name,
                thread_num=thread_num)
            res['image'] = im
            res['im_info'] = im_info
        return res

    def postprocess(self, results, topk=1, batch_size=1, im_shape=None):
        if self.model_type == "classifier":
            true_topk = min(self.num_classes, topk)
            preds = BaseClassifier._postprocess(results, true_topk,
                                                self.labels)
        elif self.model_type == "detector":
            if self.model_name == "YOLOv3":
                preds = YOLOv3._postprocess(results, ['bbox'], batch_size,
                                            self.num_classes, self.labels)
            elif self.model_name == "FasterRCNN":
                preds = FasterRCNN._postprocess(results, ['bbox'], batch_size,
                                                self.num_classes, self.labels)
            elif self.model_name == "MaskRCNN":
                preds = MaskRCNN._postprocess(
                    results, ['bbox', 'mask'], batch_size, self.num_classes,
                    self.mask_head_resolution, self.labels)

        return preds

    def raw_predict(self, inputs):
        """ 接受预处理过后的数据进行预测

            Args:
                inputs(tuple): 预处理过后的数据
        """
        for k, v in inputs.items():
            try:
                tensor = self.predictor.get_input_tensor(k)
            except:
                continue
            tensor.copy_from_cpu(v)
        self.predictor.zero_copy_run()
        output_names = self.predictor.get_output_names()
        output_results = list()
        for name in output_names:
            output_tensor = self.predictor.get_output_tensor(name)
            output_results.append(output_tensor.copy_to_cpu())
        return output_results

    def predict(self, image, topk=1):
        """ 图片预测

            Args:
                image(str|np.ndarray|list|tuple): 图像路径；或者是解码后的排列格式为（H, W, C）且类型为float32且为BGR格式的数组。
                    或者是对数（元）组中的图像同时进行预测，数组中的元素可以是图像路径，也可以是解码后的排列格式为（H，W，C）
                    且类型为float32且为BGR格式的数组。
                topk(int): 分类预测时使用，表示预测前topk的结果
        """
        preprocessed_input = self.preprocess([image])
        model_pred = self.raw_predict(preprocessed_input)
        im_shape = None if 'im_shape' in preprocessed_input else preprocessed_input[
            'im_shape']
        results = self.postprocess(
            model_pred, topk=topk, batch_size=1, im_shape=im_shape)

        return results[0]

    def batch_predict(self, image_list, topk=1, thread_num=2):
        """ 图片预测

            Args:
                image(str|np.ndarray|list|tuple): 图像路径；或者是解码后的排列格式为（H, W, C）且类型为float32且为BGR格式的数组。
                    或者是对数（元）组中的图像同时进行预测，数组中的元素可以是图像路径，也可以是解码后的排列格式为（H，W，C）
                    且类型为float32且为BGR格式的数组。
                topk(int): 分类预测时使用，表示预测前topk的结果
        """
        preprocessed_input = self.preprocess(image_list)
        model_pred = self.raw_predict(preprocessed_input)
        im_shape = None if 'im_shape' in preprocessed_input else preprocessed_input[
            'im_shape']
        results = self.postprocess(
            model_pred, topk=topk, batch_size=1, im_shape=im_shape)

        return results
