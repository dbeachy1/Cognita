# Third-party notices

Cognita is licensed under the Apache License 2.0 (see `LICENSE`). It uses the
third-party components below, under their own licenses.

This list is **partial**. It identifies selected bundled components and runtime
dependencies; a distribution still needs a review of its complete dependency,
model, and container contents before release.

## Alpine.js 3.14.9 (bundled Admin UI script)

- License: MIT. Copyright © 2019-2021 Caleb Porzio and contributors.
- Upstream: https://github.com/alpinejs/alpine/tree/v3.14.9
- Complete license text: `src/cognita/web/alpine.LICENSE.md`, packaged with the
  bundled `alpine.min.js` script.

## Pico CSS 2.1.1 (bundled Admin UI stylesheet)

- License: MIT. Copyright (c) 2019-2024 Pico.
- Upstream: https://github.com/picocss/pico/tree/v2.1.1
- Complete license text: `src/cognita/web/pico.LICENSE.md`, packaged with the
  bundled `pico.min.css` stylesheet.

## DejaVu Sans 2.37 (OCR qualification fixture)

- DejaVu changes are in the public domain; Bitstream Vera and Arev glyphs retain
  their copyright and license notices.
- Upstream: https://github.com/dejavu-fonts/dejavu-fonts/tree/version_2_37
- Complete applicable notice and font SHA-256:
  `tests/fixtures/ocr_qualification/DEJAVU-LICENSE.txt`.

## BAAI bge-reranker-v2-m3 (search reranker model)

- License: Apache License 2.0.
- Copyright: Beijing Academy of Artificial Intelligence (BAAI).
- Upstream: https://huggingface.co/BAAI/bge-reranker-v2-m3, commit
  `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`.
- Cognita uses the ONNX format conversion published at
  https://huggingface.co/onnx-community/bge-reranker-v2-m3-ONNX, revision
  `6f5ff65298512715a1e669753bc754d2bc8f367b`. That repository names
  BAAI/bge-reranker-v2-m3 as its base model and carries no license of its own; it is a
  format conversion of the Apache-2.0 weights above.
- The weights are **not** included in the Cognita image. Cognita downloads them into
  its model cache on first use, pinned to the revision above and checked against
  SHA-256 hashes recorded in `src/cognita/embeddings.py`.

## pypdfium2 (PDF text extraction)

- License: Apache License 2.0 or BSD-3-Clause, at your option.
- Project: https://github.com/pypdfium2-team/pypdfium2
- The license texts ship in the installed wheel, under
  `pypdfium2-<version>.dist-info/licenses/` in the image's Python environment.

## PDFium (bundled in the pypdfium2 wheel)

- License: BSD-3-Clause. Copyright 2014 The PDFium Authors.
- Project: https://pdfium.googlesource.com/pdfium/
- PDFium's bundled third-party components (for example FreeType, ICU, Little CMS,
  libjpeg-turbo, libpng, OpenJPEG and zlib) are under their own licenses. Their texts
  ship in the same wheel directory as above.

## NVIDIA CUDA runtime libraries, cuDNN and cuSPARSELt (NVIDIA image only)

The NVIDIA container image (`cognita-app:<version>-nvidia`) includes NVIDIA CUDA
runtime libraries (libcudart, libcublas/libcublasLt, libcufft, libcurand, libcusparse,
libcusolver, libnvrtc, libnvJitLink, libcupti, libnvToolsExt, libcufile), NVIDIA cuDNN
and NVIDIA cuSPARSELt, installed unmodified from NVIDIA's PyPI wheels. These components
are NOT covered by Cognita's Apache-2.0 license. They are proprietary software of NVIDIA
Corporation, redistributed as part of this application under:

- License Agreement for NVIDIA Software Development Kits (CUDA Toolkit EULA),
  https://docs.nvidia.com/cuda/eula/index.html
- NVIDIA cuDNN Software License Agreement,
  https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html
- NVIDIA cuSPARSELt Software License Agreement,
  https://docs.nvidia.com/cuda/cusparselt/license.html

By pulling and using the NVIDIA image you accept those NVIDIA terms. Under them the
NVIDIA components are licensed to run only on systems with NVIDIA GPUs, may be used
only by this application, and may not be extracted, modified or redistributed
separately from it. The NVIDIA driver itself is not included; it is provided by the
host and governed by NVIDIA's driver license.

## NVSHMEM, NCCL, cuda-python (NVIDIA image only)

- NVSHMEM: Apache License 2.0 (https://github.com/NVIDIA/nvshmem).
- NCCL: BSD 3-Clause (https://github.com/NVIDIA/nccl).
- cuda-bindings, cuda-pathfinder: Apache License 2.0 (https://github.com/NVIDIA/cuda-python).
