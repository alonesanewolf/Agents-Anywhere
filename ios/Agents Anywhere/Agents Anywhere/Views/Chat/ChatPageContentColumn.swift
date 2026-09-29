import SwiftUI

/// Apply to the content inside the scroll view. The scroll viewport stays full
/// width while both detail pages share the session's ordinary centered layout.
/// The chat timeline runs edge to edge; card pages keep a margin.
struct ChatPageContentColumn: ViewModifier {
    var horizontalInset: CGFloat = 24

    func body(content: Content) -> some View {
        content
            .padding(.horizontal, horizontalInset).padding(.top, 16)
            .frame(maxWidth: 760).frame(maxWidth: .infinity)
    }
}
