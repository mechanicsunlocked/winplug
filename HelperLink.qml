import QtQuick
import Quickshell.Io

// One line-oriented connection to winplugd that survives the helper going
// away and coming back (an upgrade, a crash, or not being installed yet).
//
// Quickshell 0.3.1's Socket cannot be re-dialled once a dial has failed: the
// failed QLocalSocket is kept, and every later `connected = true` finds "a
// socket exists" and does nothing (io/socket.cpp, setConnected).  A helper
// restart hits exactly that -- the first retry lands while the old socket
// file is gone or refusing, and from then on the panel says "helper not
// running" until the whole shell is restarted.  A freshly built Socket dials
// every time, so the Socket lives in a Loader and every retry replaces it.
Item {
  id: link

  // Socket path; empty means "do not dial".
  property string path: ""
  // How often to knock while not connected.
  property int retryInterval: 2000

  // The live Socket object, or null between dials.
  readonly property var socket: sockLoader.item
  // Quickshell's Socket notifies `connected` through connectionStateChanged,
  // so bind to the property; this readonly property then has a proper
  // connectedChanged of its own for users of the link.
  readonly property bool connected: socket ? socket.connected === true : false

  // One complete line from the helper, without the newline.
  signal line(string text)

  visible: false
  width: 0
  height: 0

  Loader {
    id: sockLoader
    active: link.path !== ""
    sourceComponent: Socket {
      id: s
      path: link.path
      connected: true
      parser: SplitParser {
        // A socket that is being replaced may still deliver; only the
        // current one speaks for the helper.
        onRead: function(text) { if (sockLoader.item === s) link.line(text) }
      }
    }
    // Whichever order the engine applies `path` and `connected` in, the new
    // object must end up dialling.
    onLoaded: if (item && !item.connected) item.connected = true
  }

  // The Loader made the first attempt when it was created; the timer only
  // handles the retries.
  Timer {
    interval: link.retryInterval
    repeat: true
    running: link.path !== "" && !link.connected
    onTriggered: link.redial()
  }

  // The path can change after creation (the bar injects the widget's
  // settings a moment after the widget exists); dial the new one.
  onPathChanged: redial()

  // Throw the socket object away and build a new one.  Dropping the Loader
  // this turn and re-arming it the next lets the old socket's deleteLater
  // run before the new one dials the same path.
  function redial() {
    sockLoader.active = false
    Qt.callLater(function() { if (link.path !== "") sockLoader.active = true })
  }

  // Write one line (the caller supplies the newline).  False when there is
  // nothing to write to.
  function send(text) {
    var s = link.socket
    if (!s || !s.connected) return false
    s.write(text)
    s.flush()
    return true
  }
}
