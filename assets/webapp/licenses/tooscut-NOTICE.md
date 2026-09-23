# Historical source reference and replacement notice

Upstream project: https://github.com/mohebifar/tooscut

License: Elastic License 2.0; see `tooscut-ELASTIC-LICENSE-2.0.txt`.

Inspected upstream revision: `16c995bc371e9250d440ef599f1c3a826b35bb75`.

Source reference:
https://github.com/mohebifar/tooscut/blob/16c995bc371e9250d440ef599f1c3a826b35bb75/apps/ui/src/components/editor/transform/snap.ts

An earlier version of the editor named this source in its canvas snapping
code and identified Tooscut as a reference for timeline edge scrolling.

The current `studioSnapPipPosition` and `studioAutoScrollTimeline` functions
in `../index.html` were replaced with new implementations written from
functional specifications, without consulting the upstream source during
authoring. They preserve the editor's interfaces and behavior. This is a
replacement of those two functions, not a claim that every surrounding
component or the supplied UI template has independently verified provenance.

The surrounding PIP geometry, dragging, resizing, rotation, and guide code
was compared with the named upstream revision's transform sources; no
additional directly copied block was identified in that review. This is a
bounded source review, not a guarantee about every possible third-party work.

This notice and the license are retained as a historical source record.
Their presence does not apply Elastic License 2.0 to independently implemented
code or the whole application. The project is not endorsed by the upstream
authors. See [editor-sources.md](editor-sources.md) for the other references.

Keep the upstream source attribution and full license with any redistributed
copies that retain adapted code. Elastic License 2.0 restricts hosted or managed
services that provide access to substantial functionality. Including this
notice does not waive those restrictions or grant a new license for the
rest of the application.
