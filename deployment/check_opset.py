import onnx

try:
    model_path = "mmdeploy_models/mmpose/ort/end2end.onnx"
    model = onnx.load(model_path)

    print(f"Successfully loaded ONNX model from: {model_path}")

    # ONNX models store opset imports in a list.
    # The main opset is usually for the '' domain (standard ONNX ops).
    opset_version_found = False
    for opset in model.opset_import:
        print(f"Domain: '{opset.domain}', Version: {opset.version}")
        if opset.domain == '' or opset.domain == 'ai.onnx': # Check for standard ONNX domain
            opset_version_found = True

    if not opset_version_found:
        print("Could not determine main ONNX opset version from model.opset_import")

except Exception as e:
    print(f"Error loading or inspecting ONNX model: {e}")