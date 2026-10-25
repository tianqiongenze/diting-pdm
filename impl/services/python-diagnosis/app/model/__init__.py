"""模型治理（docs/43 Phase F）：注册表 / 漂移检测 / 反馈闭环。"""

from . import registry  # noqa: F401
from .registry import (accuracy_report, current_model_id, detect_drift,  # noqa: F401
                       list_models, record_feedback, register_model)
