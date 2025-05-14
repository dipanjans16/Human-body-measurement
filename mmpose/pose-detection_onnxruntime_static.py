_base_ = ['./pose-detection_static.py', '../_base_/backends/onnxruntime.py']

onnx_config = dict(
    input_shape=None,
    opset_version=11  # <--- ADD THIS LINE
)