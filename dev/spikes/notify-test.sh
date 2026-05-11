#!/usr/bin/env bash
# Spike: prove macOS notification delivery from a script.
# Uses osascript's `display notification` — built-in, no extra dependencies.
#
# Purpose: validate that the Constellation daemon can surface pending-review
# notifications to the human via the macOS Notification Center.
#
# Usage:
#   ./notify-test.sh
#
# Permissions note: on first run, macOS may prompt for notification permission
# for "Script Editor" (the host process for osascript). Approve once; subsequent
# runs appear silently as banners in Notification Center.

set -euo pipefail

TITLE="Mem-Fusion"
SUBTITLE="Pending memory reviews"
MESSAGE="Mem-Fusion has (3) updates from Atif and Matt, Click to accept or ignore"
SOUND="Glass"

osascript -e "display notification \"$MESSAGE\" with title \"$TITLE\" subtitle \"$SUBTITLE\" sound name \"$SOUND\""

echo "✓ Notification dispatched at $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Title:    $TITLE"
echo "  Subtitle: $SUBTITLE"
echo "  Body:     $MESSAGE"
echo ""
echo "If you see a banner in the top-right corner OR an entry in Notification Center"
echo "(swipe with two fingers from the right edge of your trackpad), the spike works."
echo ""
echo "If nothing appears and this is the first time running osascript notifications,"
echo "macOS may have suppressed the banner pending permission grant — check"
echo "System Settings → Notifications → Script Editor and ensure 'Allow Notifications' is on."
