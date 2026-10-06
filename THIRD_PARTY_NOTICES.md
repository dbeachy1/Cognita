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

## Microsandbox 0.7.0 and libkrunfw (Workspace runtime image)

- The Workspace runtime contains the unmodified Microsandbox 0.7.0 Linux x86-64
  `msb` executable (Apache-2.0; SHA-256
  `bce88f9c017d0b01f785907da34b8126c1fa809838c79fed89b1abc2ca4f58a8`) and
  `libkrunfw` shared library (SHA-256
  `ce9a749e8471e89aa5e2ad88de0c1581c3384c100bcb107a75bb12739a12d590`).
  These hashes match the [official Microsandbox 0.7.0 release](https://github.com/superradcompany/microsandbox/releases/tag/v0.7.0).
- The release source is [Microsandbox commit `a427b436`](https://github.com/superradcompany/microsandbox/tree/a427b4365765cb633df3267e0cf3d118c25f1843),
  whose `vendor/libkrunfw` submodule is [commit `cf4c22b9`](https://github.com/superradcompany/libkrunfw/tree/cf4c22b9f05c680928e6d96a9d198f5845573a87).
  That firmware bundles Linux kernel 6.12.109. The firmware library code is
  LGPL-2.1-only; its patches and bundled Linux kernel are GPL-2.0-only.
- The matching Cognita release provides
  `cognita-libkrunfw-corresponding-source-v0.7.0.tar` (SHA-256
  `f7bacf91c690b723c97c7a2f6020ac2f65a553e4b1b3f188be3ae975dab84f9c`).
  It contains that `libkrunfw` revision, its build files, patches and
  configuration, and the Linux 6.12.109 source. The archive contains the
  complete license texts and source.

## NVIDIA CUDA runtime libraries, cuDNN and cuSPARSELt (NVIDIA image only)

The NVIDIA container image (`cognita-app:<version>-nvidia`) includes NVIDIA CUDA
runtime libraries (libcudart, libcublas/libcublasLt, libcufft, libcurand, libcusparse,
libcusolver, libnvrtc, libnvJitLink, libcupti, libcufile), NVIDIA cuDNN
and NVIDIA cuSPARSELt, installed unmodified from NVIDIA's PyPI wheels. These components
are NOT covered by Cognita's Apache-2.0 license. They are proprietary software of NVIDIA
Corporation, redistributed as part of this application under:

- License Agreement for NVIDIA Software Development Kits and CUDA Toolkit
  Supplement, CUDA 13.0.2:
  https://docs.nvidia.com/cuda/archive/13.0.2/eula/index.html
- NVIDIA cuDNN Software License Agreement,
  https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html
- NVIDIA cuSPARSELt Software License Agreement,
  https://docs.nvidia.com/cuda/cusparselt/license.html

The complete license texts supplied with the installed wheels remain under their
`*.dist-info/licenses/` directories and govern the versions shipped. The companion
notice `NVIDIA-CUDA-13.0.2-EULA.html` retains the versioned CUDA agreement and
redistribution appendix, including libnvJitLink and libcufile, which are absent
from the older CUDA agreement text included in those wheels.

By pulling and using the NVIDIA image you accept those NVIDIA terms. Under them the
NVIDIA components are licensed to run only on systems with NVIDIA GPUs, may be used
only by this application, and may not be extracted, modified or redistributed
separately from it. The NVIDIA driver itself is not included; it is provided by the
host and governed by NVIDIA's driver license.

## NVSHMEM 3.4.5 (NVIDIA image only)

NVSHMEM 3.4.5 is distributed under the NVIDIA Software Development Kits license
agreement and NVSHMEM supplement, which permits distribution of SDK portions as
part of an application with material additional functionality, for use on systems
with NVIDIA GPUs. Its complete versioned terms and incorporated third-party
notices are at
https://github.com/NVIDIA/nvshmem/blob/v3.4.5-0/License.txt and retained in the
companion notice `NVIDIA-NVSHMEM-3.4.5-License.txt`. The additional notices include
BSD-licensed code from DF-NVSHMEM, Sandia OpenSHMEM and other projects. Preserve
those notices along with the original wheel license text. The license of newer
NVSHMEM releases does not replace the terms for this shipped version.

## NVTX, NCCL, cuda-python (NVIDIA image only)

- NVTX (`nvidia-nvtx` 13.0.85): Apache License 2.0. The complete license and
  copyright notices ship in the wheel's `*.dist-info/licenses/License.txt`.
- NCCL: BSD 3-Clause (https://github.com/NVIDIA/nccl).
- cuda-bindings, cuda-pathfinder: Apache License 2.0 (https://github.com/NVIDIA/cuda-python).

## Cognita 16 book projection Python dependencies

The service installs `rfc8785` 0.1.4 under the Apache License 2.0 and `regex`
2026.9.29 under the package's Apache-2.0 and CNRI-Python license terms. Their
versioned license files remain in the installed wheels' `*.dist-info/licenses/`
directories. Projects: https://github.com/trailofbits/rfc8785.py and
https://github.com/mrabarnett/mrab-regex.

## FFmpeg tools and LAME runtime (Linux Cognita images)

The CPU, AMD, and NVIDIA Cognita images install the distribution-provided
`ffmpeg` package, which supplies both `ffmpeg` and `ffprobe`, and
`libmp3lame0` for MP3 encoding. FFmpeg's enabled components carry the LGPL or
GPL terms applicable to that distribution build; LAME carries its own license
terms. Keep each image's distribution copyright and license files, including
`/usr/share/doc/ffmpeg/copyright` and `/usr/share/doc/libmp3lame0/copyright`,
with the package versions recorded by release build evidence. Upstream:
https://ffmpeg.org/ and https://lame.sourceforge.io/.
