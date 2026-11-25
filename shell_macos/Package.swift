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
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("SwiftUI"),
                .linkedFramework("Carbon"),
                .linkedFramework("QuickLookUI"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("UserNotifications"),
                .linkedFramework("ServiceManagement")
            ]
        )
    ]
)
