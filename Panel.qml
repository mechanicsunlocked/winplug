import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
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
  readonly property bool helperConnected: link.connected
  // Optimistic per-device marker so a row moves the instant it is clicked
  // rather than a round trip later.  Cleared by the next state push.
  property var pending: ({})

  readonly property var devices: helperState && helperState.devices ? helperState.devices : []
  readonly property var vm: helperState ? helperState.vm : null
  readonly property string vmStatus: vm ? String(vm.status) : ""
  readonly property bool vmRunning: vmStatus === "running" || vmStatus === "running-no-usb"
  readonly property bool vmOff: vmStatus === "off"
  readonly property bool vmBusy: vmStatus === "starting" || vmStatus === "stopping"
  // Offer the button whenever the VM is installed and not mid start/stop:
  // "Open" when it is running, "Start" otherwise (off, or an odd Docker state).
  readonly property bool canLaunch: helperConnected && vm && vm.installed === true && !vmBusy
  readonly property int attachedCount: helperState && helperState.attached_count ? helperState.attached_count : 0
  readonly property int assignedCount: helperState && helperState.assigned_count ? helperState.assigned_count : 0

  // ---- Windows itself: power and autostart -----------------------------------
  //
  // The helper starts and stops the VM through Omarchy's own privileged entry
  // point, without a password prompt, but only when that launcher is
  // root-owned (the `launcher` flag).  Autostart is a line in the helper's
  // config, flipped through the same socket.
  readonly property bool canPower: helperConnected && vm && vm.installed === true && vm.launcher !== false
  // The switch shows the *desired* state: on while running or coming up, off
  // while shutting down.
  readonly property bool powerOn: vmRunning || vmStatus === "starting"
  readonly property bool autostartOn: vm && vm.autostart === true
  // Optimistic autostart value after a click, until the next state push.
  property int autostartPending: -1
  readonly property bool autostartShown: autostartPending >= 0 ? autostartPending === 1 : autostartOn
  property bool stopConfirmOpen: false
  // The question opens on "Cancel", never on the destructive button: Enter
  // on its own does not shut Windows down (the kit's default is the other way).
  onStopConfirmOpenChanged: if (stopConfirmOpen) stopConfirm.selectedIndex = 0
  // The last refused request, shown under the hero for a moment.
  property string lastError: ""

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

  // The connection itself, with its retry and re-dial logic, lives in
  // HelperLink.qml (which tools/test-reconnect.sh exercises against a helper
  // that is stopped, killed and restarted).  The helper pushes the full
  // state on connect and after every change.
  HelperLink {
    id: link
    path: root.socketPath
    onLine: function(text) { root.onLine(text) }
    onConnectedChanged: if (!connected) { root.helperState = null; root.pending = ({}) }
  }

  function onLine(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (!msg) return
    if (msg.type === "state") {
      root.helperState = msg
      root.pending = ({})
      root.autostartPending = -1
    } else if (msg.type === "reply" && msg.ok === false) {
      // A refused request: drop the optimism for that device, or for the
      // autostart switch, and say why.
      var next = {}
      for (var k in root.pending) if (k !== msg.key) next[k] = root.pending[k]
      root.pending = next
      if (msg.cmd === "set_autostart") root.autostartPending = -1
      root.showError(String(msg.cmd || "request") + ": " + String(msg.error || "failed"))
      console.warn("winplug:", msg.cmd, "failed:", msg.error)
    }
  }

  function showError(text) {
    root.lastError = text
    errorTimer.restart()
  }

  Timer {
    id: errorTimer
    interval: 12000
    onTriggered: root.lastError = ""
  }

  function request(obj) {
    link.send(JSON.stringify(obj) + "\n")
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

  // `winplug launch`: starts Windows through the helper (no password
  // prompt), waits until it answers on RDP, then opens the session.  Run as
  // an app through uwsm so it outlives this panel like any launched program.
  function launchWindows() {
    Quickshell.execDetached(["uwsm", "app", "--", "/usr/local/bin/winplug", "launch"])
  }

  // Power, through the helper.  The helper answers `vm_up`/`vm_down` when the
  // action is done (minutes, for a shutdown) and pushes "starting"/"stopping"
  // state right away, which is what the switch shows as busy meanwhile.
  function startWindows() {
    if (!root.canPower || root.vmBusy) return
    root.lastError = ""
    request({ cmd: "vm_up" })
  }
  function stopWindows() {
    root.stopConfirmOpen = false
    if (!root.canPower || root.vmBusy) return
    root.lastError = ""
    request({ cmd: "vm_down" })
  }
  // Off -> start right away; on -> ask first, a shutdown ends whatever is
  // going on in there.
  function togglePower() {
    if (!root.canPower || root.vmBusy) return
    if (root.powerOn) root.stopConfirmOpen = true
    else root.startWindows()
  }
  function toggleAutostart() {
    if (!root.canPower) return
    var on = !root.autostartShown
    root.autostartPending = on ? 1 : 0
    root.lastError = ""
    request({ cmd: "set_autostart", on: on })
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
    stopConfirmOpen = false
    if (opened) {
      selectedIndex = 0
      cursorActive = false
      actionFocused = false
      request({ cmd: "state" })
    }
  }

  // ---- the icon -----------------------------------------------------------------
  //
  // The mark from logo.svg in the bar's own colour: the four Windows panes with
  // the USB trident in the bottom-right one.  Drawn rather than borrowed: no
  // glyph says "USB into Windows", and drawing it keeps it on theme.
  //
  // Idle, all four panes are dim and the trident is full ink.  While a device is
  // in Windows the USB pane lights up solid with the trident cut out of it, so
  // whatever is behind the bar (the hover highlight too) shows through.
  //
  // A trident inside one pane needs room: under 24 device pixels (the bar's
  // 16 px at scale 1) it turns into a blob.  There the icon falls back to the
  // compact mark, the trident standing across all four panes and all four
  // panes filling in when lit.  That one was checked at 16 px: the panes
  // survive as four blocks, the trident as a stem with a fork.
  component WinplugIcon: Item {
    id: icon
    property color ink: root.foreground
    property real paneAlpha: 0.28
    property bool lit: false

    readonly property real s: Math.min(width, height)
    readonly property bool compact: s * Screen.devicePixelRatio < 24
    readonly property real gap: Math.max(1, s * 0.09)
    readonly property real pane: (s - gap) / 2
    readonly property real paneRadius: Math.max(0.5, s * 0.06)
    readonly property real stroke: Math.max(1, s * 0.11)
    readonly property color dimColor: Qt.rgba(ink.r, ink.g, ink.b, paneAlpha)
    readonly property color paneColor: Qt.rgba(ink.r, ink.g, ink.b, lit ? Math.min(1, paneAlpha + 0.32) : paneAlpha)

    // Four panes, slightly rounded like the modern Windows mark.  In the full
    // mark the fourth one is usbPane below.
    Repeater {
      model: 4
      Rectangle {
        required property int index
        visible: icon.compact || index < 3
        x: (index % 2) * (icon.pane + icon.gap)
        y: Math.floor(index / 2) * (icon.pane + icon.gap)
        width: icon.pane
        height: icon.pane
        radius: icon.paneRadius
        color: icon.compact ? icon.paneColor : icon.dimColor
      }
    }

    // ---- full mark: the trident in the bottom-right pane

    Item {
      id: usbPane
      visible: !icon.compact
      x: icon.pane + icon.gap
      y: icon.pane + icon.gap
      width: icon.pane
      height: icon.pane

      // Idle: a dim pane with the trident on it in full ink.
      Rectangle {
        anchors.fill: parent
        visible: !icon.lit
        radius: icon.paneRadius
        color: icon.dimColor
      }
      Trident {
        visible: !icon.lit
        ink: icon.ink
      }

      // Lit: a solid pane, masked by the trident turned inside out.  The mask
      // item fills the whole pane because a mask is stretched over the effect.
      Rectangle {
        id: solidPane
        anchors.fill: parent
        visible: false
        radius: icon.paneRadius
        color: icon.ink
      }
      Item {
        id: cutout
        anchors.fill: parent
        visible: false
        layer.enabled: true
        layer.smooth: true
        Trident { ink: "black" }    // only the alpha counts in a mask
      }
      MultiEffect {
        anchors.fill: parent
        visible: icon.lit
        source: solidPane
        maskEnabled: true
        maskInverted: true
        maskSource: cutout
        maskThresholdMin: 0.5
        maskSpreadAtMin: 1.0
      }
    }

    // ---- compact mark: the trident across all four panes

    Item {
      anchors.fill: parent
      visible: icon.compact

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
  }

  // The USB trident as in logo.svg, in a 100-unit box that fills its pane
  // with a small margin: arrow on top, round base, the left branch ending in
  // a circle and the right one in a square.
  component Trident: Item {
    id: tri
    property color ink: "white"
    readonly property real u: width / 100

    x: parent.width * 0.05
    y: parent.height * 0.035
    width: parent.width * 0.9
    height: width

    Shape {
      anchors.fill: parent
      preferredRendererType: Shape.CurveRenderer

      // Stem and both branches.
      ShapePath {
        strokeColor: tri.ink
        strokeWidth: 11 * tri.u
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        startX: 50 * tri.u; startY: 22 * tri.u
        PathLine { x: 50 * tri.u; y: 80 * tri.u }
        PathMove { x: 50 * tri.u; y: 66 * tri.u }
        PathLine { x: 24 * tri.u; y: 50 * tri.u }
        PathLine { x: 24 * tri.u; y: 40 * tri.u }
        PathMove { x: 50 * tri.u; y: 54 * tri.u }
        PathLine { x: 76 * tri.u; y: 38 * tri.u }
        PathLine { x: 76 * tri.u; y: 30 * tri.u }
      }
      // Arrow head.
      ShapePath {
        strokeColor: "transparent"
        fillColor: tri.ink
        startX: 50 * tri.u; startY: 3 * tri.u
        PathLine { x: 63 * tri.u; y: 25 * tri.u }
        PathLine { x: 37 * tri.u; y: 25 * tri.u }
        PathLine { x: 50 * tri.u; y: 3 * tri.u }
      }
    }
    // Round base.
    Rectangle {
      x: 39 * tri.u; y: 75 * tri.u
      width: 22 * tri.u; height: width
      radius: width / 2
      color: tri.ink
    }
    // Circle end of the left branch.
    Rectangle {
      x: 15 * tri.u; y: 24 * tri.u
      width: 18 * tri.u; height: width
      radius: width / 2
      color: tri.ink
    }
    // Square end of the right branch.
    Rectangle {
      x: 68 * tri.u; y: 15 * tri.u
      width: 16 * tri.u; height: width
      color: tri.ink
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
      // While the shutdown question is up, the keys belong to it.
      onMoveRequested: function(dx, dy) {
        if (root.stopConfirmOpen) {
          if (dx !== 0) stopConfirm.selectedIndex = stopConfirm.selectedIndex === 0 ? 1 : 0
          return
        }
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx > 0) root.actionFocused = true
        else if (dx < 0) root.actionFocused = false
      }
      onActivateRequested: {
        if (root.stopConfirmOpen) {
          if (stopConfirm.selectedIndex === 0) root.stopConfirmOpen = false
          else root.stopWindows()
          return
        }
        if (root.cursorActive) root.activateCursor()
      }
      onCloseRequested: {
        if (root.stopConfirmOpen) root.stopConfirmOpen = false
        else root.close()
      }
      onTabRequested: function(direction) {
        if (root.stopConfirmOpen) { stopConfirm.selectedIndex = stopConfirm.selectedIndex === 0 ? 1 : 0; return }
        root.switchPanel(direction)
      }
      onDeleteRequested: if (root.cursorActive && !root.stopConfirmOpen) root.deleteSelected()
      onTextKey: function(t) {
        if (root.stopConfirmOpen) return
        if ((t === "s" || t === "S") && root.canLaunch) root.launchWindows()
        if (t === "p" || t === "P") root.togglePower()
        if (t === "a" || t === "A") root.toggleAutostart()
        if (t === "r" || t === "R") root.request({ cmd: "state" })
      }

      // "Shut down Windows?" -- a click on the power switch while it is on.
      ConfirmDialog {
        id: stopConfirm
        anchors.fill: parent
        z: 10
        opened: root.stopConfirmOpen
        message: root.attachedCount > 0
          ? "Shut down Windows? " + root.attachedCount + (root.attachedCount === 1 ? " USB device is" : " USB devices are") + " in use there."
          : "Shut down Windows?"
        confirmText: "Shut down"
        foreground: root.foreground
        fontFamily: root.fontFamily
        onCanceled: root.stopConfirmOpen = false
        onConfirmed: root.stopWindows()
      }

      Column {
        id: column
        anchors.fill: parent
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          id: heroHost
          width: parent.width
          implicitHeight: hero.implicitHeight
          // Inside the hero's icon and trailing control, `root` is the
          // PanelHero, not this panel (Omarchy's own panels note the same),
          // so those reach panel state through this item.
          readonly property bool connected: root.helperConnected
          readonly property bool lit: root.attachedCount > 0
          readonly property bool showOpen: root.canLaunch
          readonly property string openHint: root.vmRunning ? "Open Windows (S)" : "Start Windows and open it (S)"
          readonly property bool showPower: root.canPower
          readonly property bool powerOn: root.powerOn
          readonly property bool busy: root.vmBusy
          readonly property color fg: root.foreground
          readonly property string font: root.fontFamily
          readonly property string powerHint: root.vmBusy
            ? (root.vmStatus === "starting" ? "Windows is starting" : "Windows is shutting down")
            : (root.powerOn ? "Shut down Windows (P)" : "Start Windows (P)")
          function openWindows() { root.launchWindows() }
          function togglePower() { root.togglePower() }

          PanelHero {
            id: hero
            width: parent.width
            title: "Winplug"
            meta: root.heroMeta
            detail: root.attachedCount > 0 ? String(root.attachedCount) : ""
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              WinplugIcon {
                width: Style.font.display
                height: Style.font.display
                ink: heroHost.fg
                lit: heroHost.lit
                opacity: heroHost.connected ? 1.0 : 0.5
              }
            }
            // Open the session (starting Windows first if it is off), and
            // the power switch.
            trailingControl: Component {
              Row {
                spacing: Style.space(10)

                PanelActionButton {
                  visible: heroHost.showOpen
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: "\uf17a"
                  tooltipText: heroHost.openHint
                  foreground: heroHost.fg
                  hoverColor: heroHost.fg
                  fontFamily: heroHost.font
                  bordered: true
                  onClicked: heroHost.openWindows()
                }

                ToggleSwitch {
                  id: powerSwitch
                  visible: heroHost.showPower
                  anchors.verticalCenter: parent.verticalCenter
                  checked: heroHost.powerOn
                  busy: heroHost.busy
                  foreground: heroHost.fg
                  onToggled: heroHost.togglePower()

                  PanelToolTip {
                    visible: powerSwitch.containsMouse
                    text: heroHost.powerHint
                    fontFamily: heroHost.font
                  }
                }
              }
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: root.lastError !== ""
          text: root.lastError
          color: root.bar ? root.bar.urgent : Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          width: parent.width
        }

        // ---------- Autostart ----------
        Toggle {
          visible: root.canPower
          width: parent.width
          label: "Start Windows at boot"
          description: root.autostartShown
            ? "Comes up in the background at boot and stays up when you close a session."
            : "Starts when you open it and shuts down when you close the session."
          checked: root.autostartShown
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.toggleAutostart()
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
            : (root.vmBusy ? "Devices go to Windows as soon as it is up."
                           : "Devices you send now go to Windows as soon as it starts.")
          color: root.dimmer
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          width: parent.width
        }

        Text {
          textFormat: Text.PlainText
          visible: root.canPower
          text: "S open · P start / shut down · A autostart · R refresh"
          color: root.dimmer
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
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
