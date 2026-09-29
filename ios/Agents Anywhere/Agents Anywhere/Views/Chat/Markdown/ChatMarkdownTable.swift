import SwiftUI
import Textual

struct ChatMarkdownTable: View {
    let rows: [[AttributedString]]
    let columns: [PresentationIntent.TableColumn]

    var body: some View {
        ScrollView(.horizontal) {
            Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                ForEach(rows.indices, id: \.self) { row in
                    GridRow {
                        ForEach(rows[row].indices, id: \.self) { column in
                            cell(rows[row][column], header: row == 0, column: column)
                                .gridColumnAlignment(alignment(column))
                        }
                    }
                    if row != rows.indices.last {
                        Divider().gridCellColumns(max(1, columns.count)).gridCellUnsizedAxes(.horizontal)
                    }
                }
            }
            // The frame hugs the grid, so a narrow table has no empty panel
            // beside it and a wide one scrolls with its border.
            .background(Color(uiColor: .secondarySystemBackground))
            .clipShape(.rect(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(.primary.opacity(0.12), lineWidth: 0.5).allowsHitTesting(false))
        }
        .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func cell(_ content: AttributedString, header: Bool, column: Int) -> some View {
        TableCellWidth(min: 56, max: 240) {
            InlineText(String(content.hashValue), parser: ParsedMarkdownText(content: content))
                .textual.textSelection(.enabled)
                .modifier(StreamingGlyphReveal())
                .fontWeight(header ? .semibold : .regular)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: Alignment(horizontal: alignment(column), vertical: .top))
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(header ? Color.primary.opacity(0.04) : .clear)
        .overlay(alignment: .trailing) {
            if column < columns.count - 1 {
                Rectangle().fill(.primary.opacity(0.12)).frame(width: 0.5).allowsHitTesting(false)
            }
        }
    }

    private func alignment(_ column: Int) -> HorizontalAlignment {
        guard columns.indices.contains(column) else { return .leading }
        return switch columns[column].alignment {
        case .left: .leading
        case .center: .center
        case .right: .trailing
        @unknown default: .leading
        }
    }
}

/// A horizontal scroll view proposes no width. A flexible frame then keeps a
/// long cell on one line past its cap, overlapping the next column. Wrap the
/// text at the capped width instead, and fill the column Grid assigns later.
private struct TableCellWidth: Layout {
    let min: CGFloat
    let max: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let content = subviews.first else { return .zero }
        let width = Swift.min(Swift.max(proposal.width.flatMap { $0.isFinite ? $0 : nil }
            ?? content.sizeThatFits(.unspecified).width, min), max)
        return CGSize(width: width, height: content.sizeThatFits(.init(width: width, height: nil)).height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: bounds.origin, anchor: .topLeading, proposal: .init(width: bounds.width, height: nil))
    }
}

struct ParsedMarkdownText: MarkupParser {
    let content: AttributedString
    func attributedString(for input: String) throws -> AttributedString { content }
}
