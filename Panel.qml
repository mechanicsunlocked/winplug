import QtQuick
import QtQuick.Shapes
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Winplug -- USB devices, and which side of the Windows VM they are on.
//
// The panel is a thin view over winplugd, the root helper that actually talks
// to QEMU.  It keeps one socket open to the helper; the helper pushes the full
// state on connect and after every change, so there is no polling here and no
// state of our own to get out of step.  A click sends one line of JSON and
// waits for the next state push, which is what moves the row.
//
// Built on Omarchy's own kit (Panel, KeyboardPanel, CursorSurface, the action
// button) so it looks like the Bluetooth panel next to it and follows the
// theme without any colours of its own.
Panel {
  id: root

  moduleName: "io.github.mechanicsunlocked.winplug"
  ipcTarget: "winplug"
  // The base Panel would register its own handler for the same target; one
  // target takes one handler, so this file owns it (see IpcHandler below).
  manageIpc: false

  readonly property string socketPath: root.setting("socketPath", "/run/winplug/winplug.sock")
  readonly property bool hideWhenNoHelper: root.setting("hideWhenNoHelper", false) === true

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color dimmer: Qt.darker(foreground, 1.55)
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "transparent"

  // ---- helper connection --------------------------------------------------

  property var helperState: null
  // Quickshell's Socket notifies `connected` through connectionStateChanged,
  // so bind to the property rather than listening for a connectedChanged
  // handler that never fires.
  readonly property bool helperConnected: sock.connected
  // Optimistic per-device marker so a row moves the instant it is clicked
  // rather than a round trip later.  Cleared by the next state push.
  property var pending: ({})

  readonly property var devices: helperState && helperState.devices ? helperState.devices : []
  readonly property var vm: helperState ? helperState.vm : null
  readonly property string vmStatus: vm ? String(vm.status) : ""
  readonly property bool vmRunning: vmStatus === "running" || vmStatus === "running-no-usb"
  readonly property bool vmOff: vmStatus === "off"
  readonly property int attachedCount: helperState && helperState.attached_count ? helperState.attached_count : 0
  readonly property int assignedCount: helperState && helperState.assigned_count ? helperState.assigned_count : 0

  function isAssigned(d) {
    var p = root.pending[d.key]
    if (p === "attach") return true
    if (p === "detach") return false
    return d.assigned === true
  }

  readonly property var inWindows: {
    var out = []
    for (var i = 0; i < devices.length; i++) if (isAssigned(devices[i])) out.push(devices[i])
    return out
  }
  readonly property var onLinux: {
    var out = []
    for (var i = 0; i < devices.length; i++) if (!isAssigned(devices[i])) out.push(devices[i])
    return out
  }
  // One flat list for the cursor: Windows side first, then Linux.
  readonly property var rows: {
    var out = []
    for (var i = 0; i < inWindows.length; i++) out.push({ dev: inWindows[i], section: "windows" })
    for (var j = 0; j < onLinux.length; j++) out.push({ dev: onLinux[j], section: "linux" })
    return out
  }

  Socket {
    id: sock
    path: root.socketPath
    parser: SplitParser {
      onRead: function(line) { root.onLine(line) }
    }
    onConnectionStateChanged: {
      if (!connected) { root.helperState = null; root.pending = ({}) }
    }
    // The path comes from shell.json, which the bar injects a moment after
    // the widget is created; the first attempt may have used the default.
    onPathChanged: Qt.callLater(root.reconnect)
  }

  // The helper may not be installed yet, or may restart; keep knocking.
  Timer {
    interval: 3000
    repeat: true
    running: !sock.connected
    triggeredOnStart: true
    onTriggered: root.reconnect()
  }

  // Quickshell keeps the *requested* state: after a failed attempt `connected`
  // reads false but the target is still true, so setting true again is a
  // no-op.  Drop the target first, then ask again.
  function reconnect() {
    if (sock.connected) return
    sock.connected = false
    sock.connected = true
  }

  function onLine(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (!msg) return
    if (msg.type === "state") {
      root.helperState = msg
      root.pending = ({})
    } else if (msg.type === "reply" && msg.ok === false) {
      // A refused request: drop the optimism for that device.
      var next = {}
      for (var k in root.pending) if (k !== msg.key) next[k] = root.pending[k]
      root.pending = next
      console.warn("winplug:", msg.cmd, "failed:", msg.error)
    }
  }

  function request(obj) {
    if (!sock.connected) return
    sock.write(JSON.stringify(obj) + "\n")
    sock.flush()
  }

  function setPending(key, what) {
    var next = {}
    for (var k in root.pending) next[k] = root.pending[k]
    next[key] = what
    root.pending = next
  }

  function sendToWindows(d) {
    if (!d) return
    setPending(d.key, "attach")
    request({ cmd: "attach", key: d.key, name: d.name })
  }

  function takeBack(d) {
    if (!d) return
    setPending(d.key, "detach")
    request({ cmd: "detach", key: d.key })
  }

  function toggleDevice(d) {
    if (!d) return
    if (isAssigned(d)) takeBack(d)
    else sendToWindows(d)
  }

  // Omarchy's own launcher, exactly as its desktop entry runs it.
  function startWindows() {
    Quickshell.execDetached(["uwsm", "app", "--", "omarchy-windows-vm", "launch"])
  }

  // ---- words ----------------------------------------------------------------

  function statusText(d) {
    var p = root.pending[d.key]
    if (p === "attach") return "Sending…"
    if (p === "detach") return "Taking back…"
    switch (String(d.status)) {
      case "attached":   return "In Windows"
      case "attaching":  return "Sending…"
      case "detaching":  return "Taking back…"
      case "waiting-vm": return root.vmOff ? "Goes to Windows when it starts" : "Waiting for Windows"
      case "unplugged":  return "Not plugged in · still assigned"
      case "no-access":  return "Blocked · restart Windows once"
      case "error":      return d.detail ? "Error: " + d.detail : "Error"
      default:           return ""
    }
  }

  function subText(d) {
    var parts = [String(d.key).toUpperCase()]
    if (d.tags && d.tags.length) parts.push(d.tags.join(" · "))
    if (d.count > 1) parts.push("×" + d.count)
    return parts.join("   ")
  }

  readonly property string heroMeta: {
    if (!root.helperConnected) return "Helper not running"
    if (!root.vm) return "Connecting"
    return String(root.vm.message)
  }

  readonly property string emptyText: {
    if (!root.helperConnected)
      return "The winplug helper is not running. Install it once with:\n\nsudo ~/.config/omarchy/plugins/io.github.mechanicsunlocked.winplug/system/install.sh"
    if (root.rows.length === 0) return "Nothing is plugged in."
    return ""
  }

  // ---- cursor -----------------------------------------------------------------

  property int selectedIndex: 0
  property bool cursorActive: false
  property bool actionFocused: false
  property string focusedKey: ""

  function moveCursor(delta) {
    if (rows.length === 0) return
    var i = selectedIndex + delta
    if (i < 0) i = 0
    if (i > rows.length - 1) i = rows.length - 1
    selectedIndex = i
    actionFocused = false
  }
  function activateCursor() {
    var r = rows[selectedIndex]
    if (r) toggleDevice(r.dev)
  }
  function deleteSelected() {
    var r = rows[selectedIndex]
    if (r && isAssigned(r.dev)) takeBack(r.dev)
  }
  function clampCursor() {
    if (selectedIndex > rows.length - 1) selectedIndex = Math.max(0, rows.length - 1)
    if (selectedIndex < 0) selectedIndex = 0
  }
  onSelectedIndexChanged: { var r = rows[selectedIndex]; focusedKey = r ? r.dev.key : "" }
  onRowsChanged: {
    // Follow the device across the two sections when it moves.
    if (focusedKey !== "") {
      for (var i = 0; i < rows.length; i++) if (rows[i].dev.key === focusedKey) { selectedIndex = i; return }
    }
    clampCursor()
  }
  onOpenedChanged: {
    if (opened) {
      selectedIndex = 0
      cursorActive = false
      actionFocused = false
      request({ cmd: "state" })
    }
  }

  // ---- the icon -----------------------------------------------------------------
  //
  // The Windows four panes with a USB trident standing in front of them.  Drawn
  // rather than borrowed: no glyph says "USB into Windows", and drawing it in
  // the bar's own colour keeps it on theme.  Checked at the bar's real 16 px:
  // the panes survive as four blocks, the trident as a stem with a fork.
  component WinplugIcon: Item {
    id: icon
    property color ink: root.foreground
    property real paneAlpha: 0.28
    property bool lit: false

    readonly property real s: Math.min(width, height)
    readonly property real gap: Math.max(1, s * 0.09)
    readonly property real pane: (s - gap) / 2
    readonly property real stroke: Math.max(1, s * 0.11)
    readonly property color paneColor: Qt.rgba(ink.r, ink.g, ink.b, lit ? Math.min(1, paneAlpha + 0.32) : paneAlpha)

    // Four panes, slightly rounded like the modern Windows mark.
    Repeater {
      model: 4
      Rectangle {
        required property int index
        x: (index % 2) * (icon.pane + icon.gap)
        y: Math.floor(index / 2) * (icon.pane + icon.gap)
        width: icon.pane
        height: icon.pane
        radius: Math.max(0.5, icon.s * 0.06)
        color: icon.paneColor
      }
    }

    // The trident: stem, fork to the left and right, and the three ends
    // (circle, square, triangle) that every USB mark carries.
    Shape {
      anchors.fill: parent
      preferredRendererType: Shape.CurveRenderer

      // Stem, bottom-centre to top-centre.
      ShapePath {
        strokeColor: icon.ink
        strokeWidth: icon.stroke
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        startX: icon.s * 0.5; startY: icon.s * 0.93
        PathLine { x: icon.s * 0.5; y: icon.s * 0.22 }
      }
      // Left branch: leaves the stem, ends in a square.
      ShapePath {
        strokeColor: icon.ink
        strokeWidth: icon.stroke
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        startX: icon.s * 0.5; startY: icon.s * 0.66
        PathLine { x: icon.s * 0.26; y: icon.s * 0.50 }
        PathLine { x: icon.s * 0.26; y: icon.s * 0.40 }
      }
      // Right branch: ends in a circle.
      ShapePath {
        strokeColor: icon.ink
        strokeWidth: icon.stroke
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        startX: icon.s * 0.5; startY: icon.s * 0.56
        PathLine { x: icon.s * 0.74; y: icon.s * 0.42 }
        PathLine { x: icon.s * 0.74; y: icon.s * 0.36 }
      }
      // The arrow head at the top of the stem.
      ShapePath {
        strokeColor: "transparent"
        fillColor: icon.ink
        startX: icon.s * 0.5; startY: icon.s * 0.04
        PathLine { x: icon.s * 0.64; y: icon.s * 0.24 }
        PathLine { x: icon.s * 0.36; y: icon.s * 0.24 }
        PathLine { x: icon.s * 0.5;  y: icon.s * 0.04 }
      }
    }
    // Square end of the left branch.
    Rectangle {
      x: icon.s * 0.26 - width / 2; y: icon.s * 0.40 - height
      width: icon.s * 0.16; height: width
      color: icon.ink
    }
    // Circle end of the right branch.
    Rectangle {
      x: icon.s * 0.74 - width / 2; y: icon.s * 0.36 - height
      width: icon.s * 0.17; height: width
      radius: width / 2
      color: icon.ink
    }
    // The plug end at the bottom of the stem.
    Rectangle {
      x: icon.s * 0.5 - width / 2; y: icon.s * 0.86
      width: icon.s * 0.22; height: icon.s * 0.14
      radius: Math.max(0.5, icon.s * 0.03)
      color: icon.ink
    }
  }

  // ---- bar button ---------------------------------------------------------------

  readonly property bool shown: !root.hideWhenNoHelper || root.helperConnected
  visible: shown
  implicitWidth: shown ? button.implicitWidth : 0
  implicitHeight: shown ? button.implicitHeight : 0

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: root.attachedCount > 0
      ? (root.attachedCount + (root.attachedCount === 1 ? " USB device" : " USB devices") + " in Windows")
      : "Winplug · USB to Windows"
    onPressed: function(b) { root.toggle() }
    iconComponent: Component {
      WinplugIcon {
        ink: button.foreground
        lit: root.attachedCount > 0
      }
    }
  }

  IpcHandler {
    target: "winplug"
    function open() { root.open() }
    function close() { root.close() }
    function toggle() { root.toggle() }
  }

  // ---- popup ---------------------------------------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx > 0) root.actionFocused = true
        else if (dx < 0) root.actionFocused = false
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onDeleteRequested: if (root.cursorActive) root.deleteSelected()
      onTextKey: function(t) {
        if ((t === "s" || t === "S") && root.vmOff) root.startWindows()
        if (t === "r" || t === "R") root.request({ cmd: "state" })
      }

      Column {
        id: column
        anchors.fill: parent
        spacing: Style.space(14)

        // ---------- Hero ----------
        PanelHero {
          title: "Winplug"
          meta: root.heroMeta
          detail: root.attachedCount > 0 ? String(root.attachedCount) : ""
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            WinplugIcon {
              width: Style.font.display
              height: Style.font.display
              ink: root.foreground
              lit: root.attachedCount > 0
              opacity: root.helperConnected ? 1.0 : 0.5
            }
          }
          trailingControl: root.helperConnected && root.vmOff ? startButton : null
        }

        Component {
          id: startButton
          PanelActionButton {
            iconText: "\uf011"
            tooltipText: "Start Windows"
            foreground: root.foreground
            hoverColor: root.foreground
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.startWindows()
          }
        }

        PanelSeparator { foreground: root.foreground }

        // ---------- Windows side ----------
        Column {
          visible: root.inWindows.length > 0
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "IN WINDOWS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }
          Repeater {
            model: root.inWindows
            DeviceRow {
              required property var modelData
              required property int index
              width: parent.width
              dev: modelData
              rowIndex: index
              windowsSide: true
            }
          }
        }

        PanelSeparator {
          visible: root.inWindows.length > 0 && root.onLinux.length > 0
          foreground: root.foreground
        }

        // ---------- Linux side ----------
        Column {
          visible: root.onLinux.length > 0
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "ON THIS MACHINE"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }
          Repeater {
            model: root.onLinux
            DeviceRow {
              required property var modelData
              required property int index
              width: parent.width
              dev: modelData
              rowIndex: root.inWindows.length + index
              windowsSide: false
            }
          }
        }

        // ---------- Notes ----------
        Text {
          textFormat: Text.PlainText
          visible: root.emptyText !== ""
          text: root.emptyText
          color: root.dimmer
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WrapAnywhere
          width: parent.width
        }

        Text {
          textFormat: Text.PlainText
          visible: root.helperConnected && root.vmStatus === "running-no-usb"
          text: "Windows was started before it had USB access. Stop it and start it again once; after that, hot-plugging just works."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
          width: parent.width
        }

        Text {
          textFormat: Text.PlainText
          visible: root.helperConnected && root.rows.length > 0
          text: root.vmRunning
            ? "Click a device to move it. Assigned devices follow every replug and every Windows start until you take them back."
            : "Devices you send now go to Windows as soon as it starts."
          color: root.dimmer
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          width: parent.width
        }
      }
    }
  }

  // Two-line device row: name, then where it is and what it is.
  component DeviceRow: CursorSurface {
    id: row
    required property var dev
    required property int rowIndex
    required property bool windowsSide

    readonly property bool rowSelected: root.cursorActive && root.selectedIndex === rowIndex
    readonly property string status: root.statusText(dev)
    readonly property bool busy: {
      var p = root.pending[dev.key]
      return p !== undefined || String(dev.status) === "attaching" || String(dev.status) === "detaching"
    }
    readonly property bool trouble: String(dev.status) === "no-access" || String(dev.status) === "error"
    readonly property bool absent: dev.present === false
    readonly property bool internal: dev.tags && (dev.tags.indexOf("bluetooth radio") >= 0 || dev.tags.indexOf("camera") >= 0)
    readonly property string actionTooltip: windowsSide ? "Take back to Linux" : (internal ? "Send to Windows (this is part of the laptop)" : "Send to Windows")

    hasCursor: rowSelected && !root.actionFocused
    current: windowsSide && String(dev.status) === "attached"
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill

    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      cursorShape: Qt.PointingHandCursor
      onContainsMouseChanged: if (containsMouse) {
        root.cursorActive = true
        root.selectedIndex = row.rowIndex
        root.actionFocused = false
      }
      onClicked: function(mouse) {
        if (mouse.button === Qt.RightButton) { if (row.windowsSide) root.takeBack(row.dev); return }
        root.toggleDevice(row.dev)
      }
    }

    PanelToolTip {
      visible: rowMouse.containsMouse && !root.actionFocused
      text: row.actionTooltip
      fontFamily: root.fontFamily
    }

    Item {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      implicitHeight: Math.max(deviceIcon.implicitHeight, info.implicitHeight, actionBtn.implicitHeight)

      Text {
        id: deviceIcon
        textFormat: Text.PlainText
        text: row.windowsSide ? "\uf17a" : "\uf287"
        color: row.absent ? root.dimmer : (row.trouble ? (root.bar ? root.bar.urgent : Color.urgent) : root.foreground)
        opacity: row.absent ? 0.6 : 1.0
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
      }

      Column {
        id: info
        spacing: Style.space(1)
        anchors.left: deviceIcon.right
        anchors.leftMargin: Style.space(10)
        anchors.right: actionBtn.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter

        Text {
          textFormat: Text.PlainText
          text: row.dev.name || "USB device"
          color: row.absent ? root.dim : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          width: parent.width
        }
        Text {
          textFormat: Text.PlainText
          visible: row.status !== ""
          text: row.status
          color: row.trouble ? (root.bar ? root.bar.urgent : Color.urgent) : (row.busy ? root.foreground : root.dim)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          width: parent.width
        }
        Text {
          textFormat: Text.PlainText
          text: root.subText(row.dev)
          color: root.dimmer
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          width: parent.width
        }
      }

      PanelActionButton {
        id: actionBtn
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        iconText: row.windowsSide ? "\uDB80\uDD59" : "\uf061"
        tooltipText: row.actionTooltip
        foreground: root.foreground
        hoverColor: root.foreground
        fontFamily: root.fontFamily
        hasCursor: row.rowSelected && root.actionFocused
        enabled: !row.busy
        onHovered: function(isHovered) {
          if (!isHovered) { if (rowMouse.containsMouse) root.actionFocused = false; return }
          root.cursorActive = true
          root.selectedIndex = row.rowIndex
          root.actionFocused = true
        }
        onClicked: root.toggleDevice(row.dev)
      }
    }
  }
}
