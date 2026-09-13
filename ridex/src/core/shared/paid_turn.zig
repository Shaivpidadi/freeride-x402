//! Whether the turn being rendered was paid for, and how much.
//!
//! The gateway reports this in-band on the stream's `response-metadata` event,
//! because a client consuming a stream never sees the response headers. The
//! fact is produced deep in the gateway client and consumed at the very top in
//! the transcript's turn summary, with nothing in between that wants to know —
//! so it lives here rather than being threaded through every layer, the same
//! way `model_provider.active_transport_provider` does.
//!
//! Single-turn state: `record` overwrites, `take` clears. If a turn is not
//! paid, nothing is recorded and the summary line is unchanged.

const std = @import("std");

/// 0.001 HBAR renders as 5 bytes; the cap is slack for larger demo amounts.
const max_amount_bytes = 32;
/// Hedera transaction ids look like `0.0.7162784@1789277817.928876698`.
const max_tx_bytes = 64;

var amount_buf: [max_amount_bytes]u8 = undefined;
var amount_len: usize = 0;
var tx_buf: [max_tx_bytes]u8 = undefined;
var tx_len: usize = 0;
var present: bool = false;

pub const Receipt = struct {
    /// Formatted by the gateway, e.g. "0.001". Empty when it did not say.
    amount_hbar: []const u8,
    /// Empty when the settlement carried no transaction id.
    transaction: []const u8,
};

/// Called by the gateway client when a response says it was paid for.
pub fn record(amount_hbar: []const u8, transaction: []const u8) void {
    amount_len = @min(amount_hbar.len, amount_buf.len);
    @memcpy(amount_buf[0..amount_len], amount_hbar[0..amount_len]);
    tx_len = @min(transaction.len, tx_buf.len);
    @memcpy(tx_buf[0..tx_len], transaction[0..tx_len]);
    present = true;
}

/// Reads and clears. The receipt belongs to one turn: leaving it set would
/// label every later free turn as paid, which is worse than saying nothing.
pub fn take() ?Receipt {
    if (!present) return null;
    present = false;
    return .{
        .amount_hbar = amount_buf[0..amount_len],
        .transaction = tx_buf[0..tx_len],
    };
}

/// Drops anything recorded but not yet rendered, so an abandoned turn cannot
/// hand its receipt to the next one.
pub fn clear() void {
    present = false;
}

test "records and clears on read" {
    clear();
    try std.testing.expect(take() == null);

    record("0.001", "0.0.7162784@1789277817.928876698");
    const receipt = take() orelse return error.TestExpectedReceipt;
    try std.testing.expectEqualStrings("0.001", receipt.amount_hbar);
    try std.testing.expectEqualStrings("0.0.7162784@1789277817.928876698", receipt.transaction);

    // One turn, one receipt.
    try std.testing.expect(take() == null);
}

test "oversized fields are truncated rather than overflowing" {
    clear();
    const long = "9" ** 200;
    record(long, long);
    const receipt = take() orelse return error.TestExpectedReceipt;
    try std.testing.expectEqual(@as(usize, max_amount_bytes), receipt.amount_hbar.len);
    try std.testing.expectEqual(@as(usize, max_tx_bytes), receipt.transaction.len);
}

test "clear discards an unread receipt" {
    clear();
    record("0.001", "tx");
    clear();
    try std.testing.expect(take() == null);
}
