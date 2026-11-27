// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Qimingxing",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "Qimingxing", targets: ["Qimingxing"])
    ],
    targets: [
        .executableTarget(
            name: "Qimingxing",
            path: "Sources/Qimingxing",
            swiftSettings: [
                .unsafeFlags([
                    "-Xfrontend", "-disable-dynamic-actor-isolation",
                    "-Xfrontend", "-disable-actor-data-race-checks",
                ])
            ],
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("SwiftUI"),
                .linkedFramework("Carbon"),
                .linkedFramework("QuickLookUI"),
                .linkedFramework("QuickLookThumbnailing"),
                .linkedFramework("Speech"),
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("UserNotifications"),
                .linkedFramework("ServiceManagement")
            ]
        ),
        .testTarget(
            name: "QimingxingTests",
            dependencies: ["Qimingxing"],
            path: "Tests/QimingxingTests"
        )
    ]
)
