# Author: Jintao Huang
# Time: 2020-5-19
import torch.nn as nn
import torch
from .backbone import EfficientNetBackBoneWithBiFPN
from .anchor import AnchorGenerator
from .classifier_regressor import Classifier, Regressor
from .loss import FocalLoss
from .efficientnet import load_params_by_order
from .utils import load_state_dict_from_url, PreProcess, PostProcess, FrozenBatchNorm2d

model_urls = {
    'efficientdet_d0':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d0.pth',
    'efficientdet_d1':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d1.pth',
    'efficientdet_d2':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d2.pth',
    'efficientdet_d3':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d3.pth',
    'efficientdet_d4':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d4.pth',
    'efficientdet_d5':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d5.pth',
    'efficientdet_d6':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d6.pth',
    'efficientdet_d7':
        'https://github.com/Jintao-Huang/EfficientDet_PyTorch/releases/download/1.0/efficientdet-d7.pth',
}

# 官方配置  official configuration
# config_dict = {
#     # resolution[% 128 == 0], backbone, fpn_channels, fpn_num_repeat, regressor_classifier_num_repeat,
#     # anchor_base_scale(anchor_size / stride)(基准尺度)
#     'efficientdet_d0': (512, 'efficientnet_b0', 64, *2, 3, 4.),  #
#     'efficientdet_d1': (640, 'efficientnet_b1', 88, *3, 3, 4.),  #
#     'efficientdet_d2': (768, 'efficientnet_b2', 112, *4, 3, 4.),  #
#     'efficientdet_d3': (896, 'efficientnet_b3', 160, *5, 4, 4.),  #
#     'efficientdet_d4': (1024, 'efficientnet_b4', 224, *6, 4, 4.),  #
#     'efficientdet_d5': (1280, 'efficientnet_b5', 288, 7, 4, 4.),
#     'efficientdet_d6': (*1408, 'efficientnet_b6', 384, 8, 5, 4.),  #
#     'efficientdet_d7': (1536, 'efficientnet_b6', 384, 8, 5, 5.)
# }


config_dict = {
    # resolution[% 128 == 0], backbone, fpn_channels, fpn_num_repeat, regressor_classifier_num_repeat,
    # anchor_base_scale(anchor_size / stride)(基准尺度)
    'efficientdet_d0': (512, 'efficientnet_b0', 64, 3, 3, 4.),  #
    'efficientdet_d1': (640, 'efficientnet_b1', 88, 4, 3, 4.),  #
    'efficientdet_d2': (768, 'efficientnet_b2', 112, 5, 3, 4.),  #
    'efficientdet_d3': (896, 'efficientnet_b3', 160, 6, 4, 4.),  #
    'efficientdet_d4': (1024, 'efficientnet_b4', 224, 7, 4, 4.),  #
    'efficientdet_d5': (1280, 'efficientnet_b5', 288, 7, 4, 4.),
    'efficientdet_d6': (1280, 'efficientnet_b6', 384, 8, 5, 4.),  #
    'efficientdet_d7': (1536, 'efficientnet_b6', 384, 8, 5, 5.)
}


class EfficientDet(nn.Module):
    def __init__(self, backbone_kwargs, num_classes,
                 regressor_classifier_num_repeat, anchor_base_scale,
                 anchor_scales=None, anchor_aspect_ratios=None, norm_layer=None):
        """please use _efficientdet()"""
        super(EfficientDet, self).__init__()

        norm_layer = norm_layer or nn.BatchNorm2d
        fpn_channels = backbone_kwargs['fpn_channels']
        self.image_size = backbone_kwargs['image_size']
        # (2^(1/3)) ^ (0|1|2)
        anchor_scales = anchor_scales or (1., 2 ** (1 / 3.), 2 ** (2 / 3.))  # scale on a single feature
        anchor_aspect_ratios = anchor_aspect_ratios or ((1., 1.), (0.7, 1.4), (1.4, 0.7))  # H, W
        num_anchor = len(anchor_scales) * len(anchor_aspect_ratios)
        self.preprocess = PreProcess(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225))
        self.backbone = EfficientNetBackBoneWithBiFPN(**backbone_kwargs)
        self.classifier = Classifier(fpn_channels, num_anchor, num_classes, regressor_classifier_num_repeat,
                                     1e-2, 1e-3, norm_layer)
        self.regressor = Regressor(fpn_channels, num_anchor, regressor_classifier_num_repeat,
                                   1e-2, 1e-3, norm_layer)
        self.anchor_gen = AnchorGenerator(anchor_base_scale, anchor_scales, anchor_aspect_ratios, [3, 4, 5, 6, 7])
        self.loss_fn = FocalLoss(alpha=0.25, gamma=2, divide_line=1 / 9)
        self.postprocess = PostProcess()

    def forward(self, image_list, targets=None, image_size=None, score_thresh=None, nms_thresh=None):
        """

        :param image_list: List[Tensor[C, H, W]]  [0., 1.]
        :param targets: Dict['labels': List[Tensor[NUMi]], 'boxes': List[Tensor[NUMi, 4]]]
            boxes: left, top, right, bottom
        :param image_size: int. 真实输入图片的大小
        :return: train模式: loss: Dict
                eval模式: result: Dict
        """
        assert isinstance(image_list[0], torch.Tensor)
        image_size = image_size or self.image_size
        # notice: anchor 32 - 812.7. Please adjust the resolution according to the specific situation
        image_size = min(1920, image_size // 128 * 128)  # 需要被128整除
        image_list, targets = self.preprocess(image_list, targets, image_size)
        x = image_list.tensors
        features = self.backbone(x)
        classifications = self.classifier(features)
        regressions = self.regressor(features)
        del features
        anchors = self.anchor_gen(x)
        # 预训练模型的顺序 -> 当前模型顺序
        # y_reg, x_reg, h_reg, w_reg -> x_reg, y_reg, w_reg, h_reg
        regressions[..., 0::2], regressions[..., 1::2] = regressions[..., 1::2], regressions[..., 0::2].clone()
        if targets is not None:
            if score_thresh is not None or nms_thresh is not None:
                print("Warning: no need to transfer score_thresh or nms_thresh")
            loss = self.loss_fn(classifications, regressions, anchors, targets)
            return loss
        else:
            score_thresh = score_thresh or 0.5
            nms_thresh = nms_thresh or 0.5
            result = self.postprocess(image_list, classifications, regressions, anchors, score_thresh, nms_thresh)
            return result


def _efficientdet(model_name, pretrained=False, progress=True,
                  num_classes=90, pretrained_backbone=True, norm_layer=None, **kwargs):
    if pretrained is True:
        norm_layer = norm_layer or FrozenBatchNorm2d
    else:
        norm_layer = norm_layer or nn.BatchNorm2d

    if pretrained:
        pretrained_backbone = False

    strict = kwargs.pop("strict", True)
    kwargs['pretrained_backbone'] = pretrained_backbone

    config = dict(zip(('image_size', 'backbone_name', 'fpn_channels', 'fpn_num_repeat',
                       "regressor_classifier_num_repeat", "anchor_base_scale"), config_dict[model_name]))
    for key, value in config.items():
        kwargs.setdefault(key, value)

    # generate backbone_kwargs
    backbone_kwargs = dict()
    for key in list(kwargs.keys()):
        if key in ("backbone_name", "pretrained_backbone", "fpn_channels", "fpn_num_repeat", "image_size"):
            backbone_kwargs[key] = kwargs.pop(key)
    backbone_kwargs['fpn_norm_layer'] = kwargs['norm_layer'] = norm_layer
    # create modules
    model = EfficientDet(backbone_kwargs, num_classes, **kwargs)
    if pretrained:
        state_dict = load_state_dict_from_url(model_urls[model_name], progress=progress)
        load_params_by_order(model, state_dict, strict)
    return model


def efficientdet_d0(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d0", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)


def efficientdet_d1(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d1", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)


def efficientdet_d2(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d2", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)


def efficientdet_d3(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d3", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)


def efficientdet_d4(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d4", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)


def efficientdet_d5(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d5", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)


def efficientdet_d6(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d6", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)


def efficientdet_d7(pretrained=False, progress=True, num_classes=90, pretrained_backbone=True,
                    norm_layer=None, **kwargs):
    return _efficientdet("efficientdet_d7", pretrained, progress, num_classes, pretrained_backbone,
                         norm_layer, **kwargs)
