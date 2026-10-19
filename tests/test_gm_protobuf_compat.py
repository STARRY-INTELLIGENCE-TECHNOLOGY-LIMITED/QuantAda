"""GM 的 protobuf label 兼容不得破坏旧版 FieldDescriptor 构造器。"""

from configs.gm import install_protobuf_field_label_compat


def test_protobuf3_constructor_can_assign_label_after_compat():
    class FieldDescriptor:
        LABEL_OPTIONAL = 1
        LABEL_REPEATED = 3

        def __init__(self, label):
            self.label = label

    assert install_protobuf_field_label_compat(FieldDescriptor) is True
    field = FieldDescriptor(2)
    assert field.label == 2
    field.label = 1
    assert field.label == 1


def test_missing_label_falls_back_to_repeated_flag():
    class FieldDescriptor:
        LABEL_OPTIONAL = 1
        LABEL_REPEATED = 3

        def __init__(self, is_repeated):
            self.is_repeated = is_repeated

    install_protobuf_field_label_compat(FieldDescriptor)
    assert FieldDescriptor(True).label == 3
    assert FieldDescriptor(False).label == 1


def test_existing_label_attribute_is_not_replaced():
    class FieldDescriptor:
        label = 7

    assert install_protobuf_field_label_compat(FieldDescriptor) is False
    assert FieldDescriptor.label == 7


def test_new_protobuf_internal_label_is_not_collapsed_to_optional():
    class FieldDescriptor:
        LABEL_OPTIONAL = 1
        LABEL_REQUIRED = 2
        LABEL_REPEATED = 3

        def __init__(self, label):
            self._label = label
            self.is_repeated = label == self.LABEL_REPEATED

    install_protobuf_field_label_compat(FieldDescriptor)
    assert FieldDescriptor(2).label == 2
    assert FieldDescriptor(3).label == 3
