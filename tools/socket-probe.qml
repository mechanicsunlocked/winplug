import QtQuick
import Quickshell

// A bare Quickshell config that hosts one HelperLink and reports what it
// sees on the console, for tools/test-reconnect.sh.  Quickshell refuses to
// load QML from outside its config directory, so the script copies this
// file and HelperLink.qml side by side into a scratch directory and runs
//
//     PROBE_SOCKET=/path/to/winplug.sock quickshell -p <dir>/socket-probe.qml
//
// Every change prints a line starting with "PROBE".
ShellRoot {
  id: probe

  readonly property string sockPath: Quickshell.env("PROBE_SOCKET") || ""
  readonly property int retryMs: parseInt(Quickshell.env("PROBE_RETRY_MS") || "500")

  Component.onCompleted: {
    var comp = Qt.createComponent(Qt.resolvedUrl("HelperLink.qml"))
    if (comp.status !== Component.Ready) {
      console.log("PROBE error " + comp.errorString())
      return
    }
    var item = comp.createObject(probe, { path: probe.sockPath, retryInterval: probe.retryMs })
    if (!item) {
      console.log("PROBE error " + comp.errorString())
      return
    }
    console.log("PROBE loaded path=" + item.path + " connected=" + item.connected)
    // PROBE_POKE=1 reproduces what the bar was seen doing: writing
    // `connected = true` to an already connected Socket.  That is a no-op on
    // the wire but arms Quickshell's internal target-connected flag, so the
    // next peer-closed makes the same Socket object re-dial at once (into a
    // helper that is not back yet) and sit wedged on the failed attempt.
    var poked = false
    item.connectedChanged.connect(function() {
      console.log("PROBE connected=" + item.connected)
      if (item.connected && !poked && Quickshell.env("PROBE_POKE") === "1") {
        poked = true
        // A tick later: the link's `socket` binding settles after `connected`.
        Qt.callLater(function() {
          if (item.socket) { item.socket.connected = true; console.log("PROBE poked") }
          else console.log("PROBE poke skipped: no socket")
        })
      }
    })
    item.line.connect(function(text) {
      console.log("PROBE line " + text)
      // Answer a state push with a request, to prove writes work too.
      if (text.indexOf('"type": "state"') >= 0 || text.indexOf('"type":"state"') >= 0)
        item.send('{"cmd":"ping"}\n')
    })
  }
}
