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
    item.connectedChanged.connect(function() { console.log("PROBE connected=" + item.connected) })
    item.line.connect(function(text) {
      console.log("PROBE line " + text)
      // Answer a state push with a request, to prove writes work too.
      if (text.indexOf('"type": "state"') >= 0 || text.indexOf('"type":"state"') >= 0)
        item.send('{"cmd":"ping"}\n')
    })
  }
}
