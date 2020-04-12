import sys

from torch import nn

from myutils.pytorch.func_util import get_optimizer, get_scheduler
from myutils.pytorch.module_util import check_if_wrapped, get_module, freeze_module_params, unfreeze_module_params
from tools.loss import KDLoss, get_single_loss, get_custom_loss
from utils.dataset_util import build_data_loaders
from utils.model_util import redesign_model, wrap_model

try:
    from apex import amp
except ImportError:
    amp = None


def extract_module(org_model, sub_model, module_path):
    if module_path.startswith('+'):
        return get_module(sub_model, module_path[1:])
    return get_module(org_model, module_path)


def set_distillation_box_info(info_dict, module_path, **kwargs):
    info_dict[module_path] = kwargs


def register_forward_hook_with_dict(module, module_path, info_dict, requires_input, requires_output):
    def forward_hook4input(self, func_input, func_output):
        info_dict[module_path]['input'] = func_input

    def forward_hook4output(self, func_input, func_output):
        info_dict[module_path]['output'] = func_output

    def forward_hook4io(self, func_input, func_output):
        info_dict[module_path]['input'] = func_input
        info_dict[module_path]['output'] = func_output

    if requires_input and not requires_output:
        return module.register_forward_hook(forward_hook4input)
    elif not requires_input and requires_output:
        return module.register_forward_hook(forward_hook4output)
    elif requires_input and requires_output:
        return module.register_forward_hook(forward_hook4io)
    raise ValueError('Either requires_input or requires_output should be True')


class DistillationBox(nn.Module):
    def setup_data_loaders(self, train_config):
        train_data_loader_config = train_config.get('train_data_loader', dict())
        val_data_loader_config = train_config.get('val_data_loader', dict())
        train_data_loader, val_data_loader =\
            build_data_loaders(self.dataset_dict, [train_data_loader_config, val_data_loader_config], self.distributed)
        if train_data_loader is not None:
            self.train_data_loader = train_data_loader
        if val_data_loader is not None:
            self.val_data_loader = val_data_loader

    @staticmethod
    def setup_hooks(model, unwrapped_org_model, model_config, info_dict):
        pair_list = list()
        forward_hook_config = model_config.get('forward_hook', dict())
        if len(forward_hook_config) == 0:
            return pair_list

        input_module_path_set = set(forward_hook_config.get('input', list()))
        output_module_path_set = set(forward_hook_config.get('output', list()))
        for target_module_path in input_module_path_set.union(output_module_path_set):
            requires_input = target_module_path in input_module_path_set
            requires_output = target_module_path in output_module_path_set
            target_module = extract_module(unwrapped_org_model, model, target_module_path)
            handle = register_forward_hook_with_dict(target_module, target_module_path, info_dict,
                                                     requires_input, requires_output)
            pair_list.append((target_module_path, handle))
        return pair_list

    def setup_loss(self, train_config):
        criterion_config = train_config['criterion']
        org_term_config = criterion_config.get('org_term', dict())
        org_criterion_config = org_term_config.get('criterion', dict()) if isinstance(org_term_config, dict) else None
        self.org_criterion = None if org_criterion_config is None or len(org_criterion_config) == 0 \
            else get_single_loss(org_criterion_config)
        self.criterion = get_custom_loss(criterion_config)
        self.use_teacher_output = self.org_criterion is not None and isinstance(self.org_criterion, KDLoss)

    def setup(self, train_config):
        # Set up train and val data loaders
        self.setup_data_loaders(train_config)

        # Define teacher and student models used in this stage
        unwrapped_org_teacher_model =\
            self.org_teacher_model.module if check_if_wrapped(self.org_teacher_model) else self.org_teacher_model
        unwrapped_org_student_model = \
            self.org_student_model.module if check_if_wrapped(self.org_student_model) else self.org_student_model
        self.target_teacher_pairs.clear()
        self.target_student_pairs.clear()
        teacher_config = train_config.get('teacher', dict())
        self.teacher_model = redesign_model(unwrapped_org_teacher_model, teacher_config, 'teacher')
        student_config = train_config.get('student', dict())
        self.student_model = redesign_model(unwrapped_org_student_model, student_config, 'student')
        self.target_teacher_pairs.extend(self.setup_hooks(self.teacher_model, unwrapped_org_teacher_model,
                                                          teacher_config, self.teacher_info_dict))
        self.target_student_pairs.extend(self.setup_hooks(self.student_model, unwrapped_org_student_model,
                                                          student_config, self.student_info_dict))

        # Define loss function used in this stage
        self.setup_loss(train_config)

        # Wrap models if necessary
        self.teacher_model =\
            wrap_model(self.teacher_model, teacher_config, self.device, self.device_ids, self.distributed)
        self.student_model =\
            wrap_model(self.student_model, student_config, self.device, self.device_ids, self.distributed)
        if not teacher_config.get('requires_grad', True):
            print('Freezing the whole teacher model')
            freeze_module_params(self.teacher_model)

        if not student_config.get('requires_grad', True):
            print('Freezing the whole student model')
            freeze_module_params(self.student_model)

        # Set up optimizer and scheduler
        optim_config = train_config['optimizer']
        self.optimizer = get_optimizer(self.student_model, optim_config['type'], optim_config['params'])
        scheduler_config = train_config.get('scheduler', None)
        self.lr_scheduler = None if scheduler_config is None \
            else get_scheduler(self.optimizer, scheduler_config['type'], scheduler_config['params'])

        # Set up apex if you require mixed-precision training
        self.apex = False
        apex_config = train_config.get('apex', None)
        if apex_config is not None and apex_config.get('requires', False):
            if sys.version_info < (3, 0):
                raise RuntimeError('Apex currently only supports Python 3. Aborting.')
            if amp is None:
                raise RuntimeError('Failed to import apex. Please install apex from https://www.github.com/nvidia/apex '
                                   'to enable mixed-precision training.')
            self.student_model, self.optimizer =\
                amp.initialize(self.student_model, self.optimizer, opt_level=apex_config['opt_level'])
            self.apex = True

    def __init__(self, teacher_model, student_model, dataset_dict, train_config, device, device_ids, distributed):
        super().__init__()
        self.org_teacher_model = teacher_model
        self.org_student_model = student_model
        self.dataset_dict = dataset_dict
        self.device = device
        self.device_ids = device_ids
        self.distributed = distributed
        self.teacher_model = None
        self.student_model = None
        self.target_teacher_pairs, self.target_student_pairs = list(), list()
        self.teacher_info_dict, self.student_info_dict = dict(), dict()
        self.train_data_loader, self.val_data_loader, self.optimizer, self.lr_scheduler = None, None, None, None
        self.org_criterion, self.criterion, self.use_teacher_output = None, None, None
        self.apex = None
        self.setup(train_config)
        self.num_epochs = train_config['num_epochs']

    def pre_process(self, epoch=None, **kwargs):
        self.teacher_model.eval()
        self.student_model.train()
        if self.distributed and self.train_data_loader.sampler is not None:
            self.train_data_loader.sampler.set_epoch(epoch)

    def check_if_org_loss_required(self):
        return self.org_criterion is not None

    @staticmethod
    def extract_outputs(model_info_dict):
        model_output_dict = dict()
        for module_path, model_io_dict in model_info_dict.items():
            sub_model_io_dict = dict()
            for key in model_io_dict.keys():
                sub_model_io_dict[key] = model_io_dict.pop(key)
            model_output_dict[module_path] = sub_model_io_dict
        return model_output_dict

    def forward(self, sample_batch, targets):
        teacher_outputs = self.teacher_model(sample_batch)
        student_outputs = self.student_model(sample_batch)
        org_loss_dict = dict()
        if self.check_if_org_loss_required():
            # Models with auxiliary classifier returns multiple outputs
            if isinstance(student_outputs, (list, tuple)):
                if self.use_teacher_output:
                    for i, sub_student_outputs, sub_teacher_outputs in enumerate(zip(student_outputs, teacher_outputs)):
                        org_loss_dict[i] = self.org_criterion(sub_student_outputs, sub_teacher_outputs, targets)
                else:
                    for i, sub_outputs in enumerate(student_outputs):
                        org_loss_dict[i] = self.org_criterion(sub_outputs, targets)
            else:
                org_loss = self.org_criterion(student_outputs, teacher_outputs, targets) if self.use_teacher_output\
                    else self.org_criterion(student_outputs, targets)
                org_loss_dict = {0: org_loss}

        output_dict = {'teacher': self.extract_outputs(self.teacher_info_dict),
                       'student': self.extract_outputs(self.student_info_dict)}
        total_loss = self.criterion(output_dict, org_loss_dict)
        return total_loss

    def update_params(self, loss):
        self.optimizer.zero_grad()
        if self.apex:
            with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        self.optimizer.step()

    def post_process(self, **kwargs):
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()

    def clean_modules(self):
        unfreeze_module_params(self.org_teacher_model)
        unfreeze_module_params(self.org_student_model)
        self.teacher_info_dict.clear()
        self.student_info_dict.clear()
        for _, module_handle in self.target_teacher_pairs + self.target_student_pairs:
            module_handle.remove()


class MultiStagesDistillationBox(DistillationBox):
    def __init__(self, teacher_model, student_model, data_loader_dict, train_config, device, device_ids, distributed):
        stage1_config = train_config['stage1']
        super().__init__(teacher_model, student_model, data_loader_dict, stage1_config, device, device_ids, distributed)
        self.train_config = train_config
        self.stage_number = 1
        self.stage_end_epoch = stage1_config['num_epochs']
        self.num_epochs = sum(train_config[key]['num_epochs'] for key in train_config.keys() if key.startswith('stage'))
        self.current_epoch = 0
        print('Started stage {}'.format(self.stage_number))

    def advance_to_next_stage(self):
        self.clean_modules()
        self.stage_number += 1
        next_stage_config = self.train_config['stage{}'.format(self.stage_number)]
        self.setup(next_stage_config)
        self.stage_end_epoch += next_stage_config['num_epochs']
        print('Advanced to stage {}'.format(self.stage_number))

    def post_process(self, **kwargs):
        super().post_process()
        self.current_epoch += 1
        if self.current_epoch == self.stage_end_epoch and self.current_epoch < self.num_epochs:
            self.advance_to_next_stage()


def get_distillation_box(teacher_model, student_model, data_loader_dict, train_config, device, device_ids, distributed):
    if 'stage1' in train_config:
        return MultiStagesDistillationBox(teacher_model, student_model, data_loader_dict,
                                          train_config, device, device_ids, distributed)
    return DistillationBox(teacher_model, student_model, data_loader_dict, train_config,
                           device, device_ids, distributed)
