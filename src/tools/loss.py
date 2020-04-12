import torch
from torch import nn
from torch.nn.functional import adaptive_max_pool2d

from myutils.pytorch import func_util

SINGLE_LOSS_CLASS_DICT = dict()
CUSTOM_LOSS_CLASS_DICT = dict()


def register_single_loss(cls):
    SINGLE_LOSS_CLASS_DICT[cls.__name__] = cls
    return cls


def register_custom_loss(cls):
    CUSTOM_LOSS_CLASS_DICT[cls.__name__] = cls
    return cls


class SimpleLossWrapper(nn.Module):
    def __init__(self, single_loss, params_config):
        super().__init__()
        self.single_loss = single_loss
        self.teacher_output_key = params_config['teacher_output']
        self.student_output_key = params_config['student_output']

    def forward(self, student_io_dict, teacher_io_dict, *args, **kwargs):
        return self.single_loss(student_io_dict[self.student_output_key]['output'],
                                teacher_io_dict[self.teacher_output_key]['output'], *args, **kwargs)


@register_single_loss
class KDLoss(nn.KLDivLoss):
    def __init__(self, temperature, alpha=None, reduction='batchmean', **kwargs):
        super().__init__(reduction=reduction)
        self.kldiv_loss = nn.KLDivLoss(reduction=reduction)
        self.temperature = temperature
        self.alpha = alpha
        cel_reduction = 'mean' if reduction == 'batchmean' else reduction
        self.cross_entropy_loss = nn.CrossEntropyLoss(reduction=cel_reduction, **kwargs)

    def forward(self, student_output, teacher_output, labels=None):
        soft_loss = super().forward(torch.log_softmax(student_output / self.temperature, dim=1),
                                    torch.softmax(teacher_output / self.temperature, dim=1))
        if self.alpha is None or self.alpha == 0 or labels is None:
            return soft_loss

        hard_loss = self.cross_entropy_loss(student_output, labels)
        return self.alpha * hard_loss + (1 - self.alpha) * (self.temperature ** 2) * soft_loss


@register_single_loss
class FSPLoss(nn.Module):
    def __init__(self, fsp_pairs, **kwargs):
        super().__init__()
        self.fsp_pairs = fsp_pairs

    @staticmethod
    def extract_feature_map(io_dict, feature_map_config):
        key = list(feature_map_config.keys())[0]
        return io_dict[feature_map_config[key]][key]

    @staticmethod
    def compute_fsp_matrix(first_feature_map, second_feature_map):
        first_h, first_w = first_feature_map.shape[2:4]
        second_h, second_w = first_feature_map.shape[2:4]
        target_h, target_w = min(first_h, second_h), min(first_w, second_w)
        if first_h > target_h or first_w > target_w:
            first_feature_map = adaptive_max_pool2d(first_feature_map, (target_h, target_w))

        if second_h > target_h or second_w > target_w:
            second_feature_map = adaptive_max_pool2d(second_feature_map, (target_h, target_w))

        first_feature_map = first_feature_map.flatten(2)
        second_feature_map = second_feature_map.flatten(2)
        hw = first_feature_map.shape[2]
        return torch.matmul(first_feature_map, second_feature_map.transpose(1, 2)) / hw

    def forward(self, student_io_dict, teacher_io_dict):
        fsp_loss = 0
        batch_size = None
        for pair_name, pair_config in self.fsp_pairs.items():
            student_first_feature_map = self.extract_feature_map(student_io_dict, pair_config['student_first'])
            student_second_feature_map = self.extract_feature_map(student_io_dict, pair_config['student_second'])
            student_fsp_matrices = self.compute_fsp_matrix(student_first_feature_map, student_second_feature_map)
            teacher_first_feature_map = self.extract_feature_map(teacher_io_dict, pair_config['teacher_first'])
            teacher_second_feature_map = self.extract_feature_map(teacher_io_dict, pair_config['teacher_second'])
            teacher_fsp_matrices = self.compute_fsp_matrix(teacher_first_feature_map, teacher_second_feature_map)
            factor = pair_config.get('factor', 1)
            fsp_loss += factor * (student_fsp_matrices - teacher_fsp_matrices).sqrt().pow(2)
            if batch_size is None:
                batch_size = student_first_feature_map.shape[0]
        return fsp_loss / batch_size


def get_single_loss(single_criterion_config, params_config=None):
    loss_type = single_criterion_config['type']
    single_loss = SINGLE_LOSS_CLASS_DICT[loss_type](**single_criterion_config['params']) \
        if loss_type in SINGLE_LOSS_CLASS_DICT else func_util.get_loss(loss_type, single_criterion_config['params'])
    return single_loss if params_config is None else SimpleLossWrapper(single_loss, params_config)


class CustomLoss(nn.Module):
    def __init__(self, criterion_config):
        super().__init__()
        term_dict = dict()
        sub_terms_config = criterion_config.get('sub_terms', None)
        if sub_terms_config is not None:
            for loss_name, loss_config in sub_terms_config.items():
                sub_criterion_config = loss_config['criterion']
                sub_criterion = get_single_loss(sub_criterion_config, loss_config.get('params', None))
                term_dict[loss_name] = (sub_criterion, loss_config['factor'])
        self.term_dict = term_dict

    def forward(self, *args, **kwargs):
        raise NotImplementedError('forward function is not implemented')


@register_custom_loss
class GeneralizedCustomLoss(CustomLoss):
    def __init__(self, criterion_config):
        super().__init__(criterion_config)
        self.org_loss_factor = criterion_config['org_term'].get('factor', None)

    def forward(self, output_dict, org_loss_dict):
        loss_dict = dict()
        student_output_dict = output_dict['student']
        teacher_output_dict = output_dict['teacher']
        for loss_name, (criterion, factor) in self.term_dict.items():
            loss_dict[loss_name] = factor * criterion(student_output_dict, teacher_output_dict)

        sub_total_loss = sum(loss for loss in loss_dict.values()) if len(loss_dict) > 0 else 0
        if self.org_loss_factor is None or self.org_loss_factor == 0:
            return sub_total_loss
        return sub_total_loss + self.org_loss_factor * sum(org_loss_dict.values() if len(org_loss_dict) > 0 else [])


def get_custom_loss(criterion_config):
    criterion_type = criterion_config['type']
    if criterion_type in CUSTOM_LOSS_CLASS_DICT:
        return CUSTOM_LOSS_CLASS_DICT[criterion_type](criterion_config)
    raise ValueError('criterion_type `{}` is not expected'.format(criterion_type))
