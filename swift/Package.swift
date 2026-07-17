// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "axstream-bar",
    platforms: [.macOS(.v14)],
    dependencies: [
        // 0.15.x for the streaming ASR API (StreamingEouAsrManager).
        .package(url: "https://github.com/FluidInference/FluidAudio.git",
                 from: "0.15.5"),
    ],
    targets: [
        .executableTarget(
            name: "axstream-bar",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio"),
            ],
            path: "Sources/axstream-bar"
        ),
    ]
)
