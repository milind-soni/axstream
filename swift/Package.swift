// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "axstream-bar",
    platforms: [.macOS(.v14)],
    dependencies: [
        // Same revision BlueyLite pins — known to build + run on this machine.
        .package(url: "https://github.com/FluidInference/FluidAudio.git",
                 revision: "fe1686fe79401cb6aec08991a320b82e3a66bb7a"),
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
