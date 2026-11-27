import XCTest
@testable import Qimingxing

/// Phase 0 smoke test: proves `swift test` can import the executable module.
/// If this target ever needs to move to a library, keep this assertion.
final class ModuleSmokeTests: XCTestCase {
    func testDesignTokensAreReachableFromTests() {
        XCTAssertEqual(QMXTheme.cardRadius, 14)
        XCTAssertEqual(QMXTheme.controlRadius, 9)
    }
}
