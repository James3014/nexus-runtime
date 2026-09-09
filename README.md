# nexus-runtime

Independent source package for task planning/admission, execution coordination,
retry/replan state, writer/event boundaries, and durable context storage.

The runtime is assembled from explicit typed bindings. Core contracts, learning,
Open SWE execution, and repository intelligence remain external package owners.
No provider or native service is selected implicitly.

The ten-module runtime spine is generated from frozen source revision
`471a281badda342ccab26606e0c46cbca6867cbb`. Planner/admission, support, and
context packages retain their donor provenance and are not relabeled as that
revision. See `docs/runtime-source-manifest.json` and
`docs/EXTRACTION_STATUS.md`.
