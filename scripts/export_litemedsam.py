"""Export the upstream LiteMedSAM checkpoint to CPU ONNX, with parity checks."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import onnxruntime as ort
import torch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    checkpoint_sha = hashlib.sha256(args.checkpoint.read_bytes()).hexdigest()
    if revision != "b0fab476e54e631dd412b25e0db9fdf2a2b0f54c":
        raise ValueError("Unexpected upstream source revision")
    if checkpoint_sha != "79d8c9dca6db4d69d3f905579e5250af05e859fff9c1f543e89a513c3028ce76":
        raise ValueError("Unexpected upstream checkpoint checksum")
    sys.path.insert(0, str(args.source))
    from tiny_vit_sam import TinyViT
    from segment_anything.modeling import MaskDecoder, PromptEncoder, TwoWayTransformer

    torch.set_num_threads(2)
    torch.manual_seed(42)
    encoder = TinyViT(img_size=256, in_chans=3, embed_dims=[64, 128, 160, 320],
        depths=[2, 2, 6, 2], num_heads=[2, 4, 5, 10], window_sizes=[7, 7, 14, 7],
        mlp_ratio=4., drop_rate=0., drop_path_rate=0., use_checkpoint=False,
        mbconv_expand_ratio=4., local_conv_size=3, layer_lr_decay=0.8)
    prompt = PromptEncoder(embed_dim=256, image_embedding_size=(64, 64),
                           input_image_size=(256, 256), mask_in_chans=16)
    mask = MaskDecoder(num_multimask_outputs=3, transformer=TwoWayTransformer(
        depth=2, embedding_dim=256, mlp_dim=2048, num_heads=8), transformer_dim=256,
        iou_head_depth=3, iou_head_hidden_dim=256)
    weights = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    for name, module in (("image_encoder", encoder), ("prompt_encoder", prompt), ("mask_decoder", mask)):
        module.load_state_dict({key.removeprefix(name + "."): value for key, value in weights.items() if key.startswith(name + ".")}, strict=True)
        module.eval()

    class Decoder(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.prompt, self.mask = prompt, mask

        def forward(self, embedding, boxes):
            sparse, dense = self.prompt(points=None, boxes=boxes, masks=None)
            return self.mask(image_embeddings=embedding, image_pe=self.prompt.get_dense_pe(),
                sparse_prompt_embeddings=sparse, dense_prompt_embeddings=dense, multimask_output=False)[0]

    decoder = Decoder().eval()
    args.output.mkdir(parents=True, exist_ok=True)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    errors = {}
    with torch.no_grad():
        image = torch.rand(1, 3, 256, 256)
        embedding = encoder(image)
        boxes = torch.tensor([[40., 50., 180., 200.]])
        for name, model, inputs, names, output in (
            ("encoder", encoder, (image,), ["image"], "embedding"),
            ("decoder", decoder, (embedding, boxes), ["embedding", "boxes"], "logits"),
        ):
            path = args.output / (name + ".onnx")
            torch.onnx.export(model, inputs, str(path), input_names=names, output_names=[output], opset_version=17)
            session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
            for sample in range(3):
                current_image = torch.rand(1, 3, 256, 256)
                current_inputs = (current_image,) if name == "encoder" else (encoder(current_image), boxes + sample * 3)
                expected = model(*current_inputs).numpy()
                actual = session.run(None, {key: value.numpy() for key, value in zip(names, current_inputs)})[0]
                np.testing.assert_allclose(actual, expected, rtol=0.005, atol=0.002)
                errors[name] = max(errors.get(name, 0), float(np.max(np.abs(actual - expected))))
            del session
    shutil.copyfile(args.source / "LICENSE", args.output / "LICENSE")
    manifest = {"id": "litemedsam-onnx", "name": "LiteMedSAM", "upstream": "https://github.com/bowang-lab/MedSAM/tree/LiteMedSAM",
        "source_revision": revision, "checkpoint_sha256": checkpoint_sha,
        "checkpoint_source": "https://drive.google.com/file/d/18Zed-TUTsmr2zc5CHUWd5Tu13nb6vq6z/view",
        "license": "Apache-2.0 upstream repository; confirm intended data and checkpoint use before clinical/commercial release",
        "precision": "float32", "opset": 17, "torch_version": torch.__version__, "onnxruntime_version": ort.__version__,
        "conversion_max_absolute_error": errors, "clinical_validation": False,
        "files": {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest() for name in ("encoder.onnx", "decoder.onnx", "LICENSE")}}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
