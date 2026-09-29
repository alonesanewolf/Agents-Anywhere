import SwiftUI

/// Apply to the content inside the scroll view. The scroll viewport stays full
/// width while both detail pages share the session's ordinary centered layout.
/// The inset matches the system layout margin, so text lines up with the
/// outer edges of the navigation bar buttons.
struct ChatPageContentColumn: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 16).padding(.top, 16)
            .frame(maxWidth: 760).frame(maxWidth: .infinity)
    }
}
