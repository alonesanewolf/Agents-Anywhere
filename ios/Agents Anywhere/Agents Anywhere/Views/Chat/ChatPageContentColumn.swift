import SwiftUI

/// Apply to the content inside the scroll view. The scroll viewport stays full
/// width while both detail pages share the session's ordinary centered layout.
/// A narrow inset gives chat text most of the phone's width.
struct ChatPageContentColumn: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 10).padding(.top, 16)
            .frame(maxWidth: 760).frame(maxWidth: .infinity)
    }
}
