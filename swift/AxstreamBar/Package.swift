// swift-tools-version:5.9
// Axstream Bar — native menu-bar voice launcher for axstream macros.
//
// Links whisper.cpp statically from the sibling checkout's cmake build:
//   ../../..//whisper.cpp/build  (see docs: cmake -B build -DBUILD_SHARED_LIBS=OFF
//   -DGGML_METAL_EMBED_LIBRARY=ON && cmake --build build)
import PackageDescription

let whisperBuild = "../../../whisper.cpp/build"

let package = Package(
    name: "AxstreamBar",
    platforms: [.macOS(.v14)],
    targets: [
        .target(
            name: "CWhisper",
            path: "Sources/CWhisper"
        ),
        .executableTarget(
            name: "AxstreamBar",
            dependencies: ["CWhisper"],
            path: "Sources/AxstreamBar",
            linkerSettings: [
                .unsafeFlags([
                    "-L\(whisperBuild)/src",
                    "-L\(whisperBuild)/ggml/src",
                    "-L\(whisperBuild)/ggml/src/ggml-metal",
                    "-L\(whisperBuild)/ggml/src/ggml-blas",
                    "-lwhisper", "-lggml", "-lggml-base", "-lggml-cpu",
                    "-lggml-metal", "-lggml-blas", "-lc++",
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Info.plist",
                ]),
                .linkedFramework("Accelerate"),
                .linkedFramework("Metal"),
                .linkedFramework("MetalKit"),
                .linkedFramework("Foundation"),
                .linkedFramework("AppKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("ApplicationServices"),
            ]
        ),
    ]
)
