import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Per-monitor presentation for the singleton composer service.
// Bar icon is created first and does not depend on the overlay loading.
Panel {
  id: root
  moduleName: "nodoom.composer"
  ipcTarget: "nodoom.composer"

  readonly property var npost: bar && bar.shell
    ? bar.shell.serviceFor("nodoom.composer") : null
  readonly property bool serviceReady: npost !== null
  readonly property bool posting: serviceReady && npost.posting
  readonly property string actionLabel: serviceReady
    ? npost.actionLabel : "Continue in Nodoom"
  readonly property string modeText: serviceReady
    ? npost.modeLabel : "Service unavailable"
  readonly property string statusText: serviceReady
    ? npost.statusText : "The Nodoom composer service did not start."
  readonly property bool statusError: !serviceReady || npost.statusError
  readonly property bool canSubmit: serviceReady && npost.ready
    && !npost.posting && String(npost.draft || "").trim().length > 0
  readonly property int charCount: serviceReady ? String(npost.draft || "").length : 0
  readonly property int charLimit: serviceReady && typeof npost.maxText === "number"
    ? npost.maxText : 5000
  readonly property bool pendingReplace: serviceReady
    && String(npost.pendingPrefill || "").length > 0

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.4)
  readonly property string family: bar ? bar.fontFamily : Style.font.family
  readonly property string serifFamily: "serif"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (opened && serviceReady) npost.refreshMode()
  }

  function submit() {
    if (serviceReady) npost.submit()
  }

  function dismiss() {
    if (serviceReady) npost.declinePrefill()
    root.close()
  }

  function compose(text) {
    var result = serviceReady ? npost.compose(text) : "service-unavailable"
    root.open()
    return result
  }

  IpcHandler {
    target: "nodoom.composer.compose"
    function compose(text: string): string { return root.compose(text) }
  }

  Connections {
    target: root.serviceReady ? root.npost : null
    function onClosePanelsRequested() { root.close() }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "N"
    tooltipText: "Compose on Nodoom"
    onPressed: root.toggle()

    iconComponent: Component {
      Item {
        Text {
          anchors.centerIn: parent
          visible: logo.status !== Image.Ready
          text: "N"
          color: button.foreground
          font.pixelSize: Math.max(10, parent.height * 0.7)
          font.bold: true
        }

        Image {
          id: logo
          anchors.fill: parent
          source: Qt.resolvedUrl("nodoom.png")
          sourceSize.width: 64
          sourceSize.height: 64
          fillMode: Image.PreserveAspectFit
          visible: false
          layer.enabled: true
        }

        MultiEffect {
          anchors.fill: logo
          visible: logo.status === Image.Ready
          source: logo
          colorization: 1.0
          colorizationColor: button.foreground
        }
      }
    }
  }

  // Isolated so an overlay/QML error cannot unmount the bar icon.
  Loader {
    id: overlayLoader
    active: true
    sourceComponent: overlayComponent
  }

  Component {
    id: overlayComponent

    KeyboardPanel {
      id: panel
      anchorItem: button
      owner: root
      bar: root.bar
      open: root.opened
      focusTarget: composer
      contentWidth: panel.fittedContentWidth(Style.space(4000))
      contentHeight: panel.fittedContentHeight(Style.space(4000), Style.space(4000))

      Item {
        id: panelBody
        anchors.fill: parent

        Rectangle {
          anchors.fill: parent
          color: "#000000"
        }

        Item {
          id: sheet
          width: Math.min(parent.width - 80, 720)
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          anchors.topMargin: 56
          anchors.bottomMargin: 40

          Item {
            id: header
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: headerLabel.implicitHeight

            Text {
              id: headerLabel
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "New post"
              color: "#f4f1ea"
              font.family: root.serifFamily
              font.pixelSize: 28
            }

            Text {
              anchors.left: headerLabel.right
              anchors.leftMargin: Style.spacing.controlGap
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              horizontalAlignment: Text.AlignRight
              elide: Text.ElideRight
              text: root.modeText
              color: root.serviceReady ? "#8a8580" : Color.urgent
              font.family: root.serifFamily
              font.pixelSize: 14
            }
          }

          TextArea {
            id: composer
            anchors.top: header.bottom
            anchors.topMargin: 28
            anchors.bottom: footer.top
            anchors.bottomMargin: 24
            anchors.left: parent.left
            anchors.right: parent.right
            text: root.serviceReady ? root.npost.draft : ""
            enabled: root.serviceReady
            readOnly: !root.serviceReady || !root.npost.ready || root.posting
            wrapMode: TextEdit.Wrap
            placeholderText: "What's on your mind?"
            color: "#f4f1ea"
            selectionColor: "#3a3530"
            selectedTextColor: "#f4f1ea"
            placeholderTextColor: "#6e6a64"
            font.family: root.serifFamily
            font.pixelSize: 24
            leftPadding: 0
            rightPadding: 0
            topPadding: 0
            bottomPadding: 0
            selectByMouse: true
            Accessible.name: "Post text"
            background: Rectangle { color: "transparent" }

            onTextChanged: {
              if (text.length > root.charLimit) {
                text = text.substring(0, root.charLimit)
                return
              }
              if (root.serviceReady && text !== root.npost.draft) {
                if (root.pendingReplace) root.npost.declinePrefill()
                root.npost.setDraft(text)
              }
            }

            Keys.onEscapePressed: function(event) {
              event.accepted = true
              root.dismiss()
            }
          }

          Item {
            id: footer
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: postButton.implicitHeight

            Button {
              id: closeButton
              anchors.left: parent.left
              text: "Close"
              focusable: true
              foreground: "#f4f1ea"
              fontFamily: root.serifFamily
              fontSize: 14
              enabled: !root.posting
              onClicked: root.dismiss()
            }

            Text {
              anchors.left: closeButton.right
              anchors.leftMargin: Style.spacing.controlGap
              anchors.right: root.pendingReplace ? keepButton.left : postButton.left
              anchors.rightMargin: Style.spacing.controlGap
              anchors.verticalCenter: parent.verticalCenter
              elide: Text.ElideRight
              text: root.statusText !== ""
                ? root.statusText
                : (root.charCount + " / " + root.charLimit)
              color: root.statusError ? Color.urgent : "#8a8580"
              font.family: root.serifFamily
              font.pixelSize: 13
            }

            Button {
              id: keepButton
              anchors.right: replaceButton.left
              anchors.rightMargin: Style.spacing.controlGap
              visible: root.pendingReplace
              text: "Keep"
              focusable: true
              foreground: "#f4f1ea"
              fontFamily: root.serifFamily
              fontSize: 14
              enabled: root.serviceReady && !root.posting
              onClicked: root.npost.declinePrefill()
            }

            Button {
              id: replaceButton
              anchors.right: parent.right
              visible: root.pendingReplace
              text: "Replace"
              selected: true
              focusable: true
              foreground: "#f4f1ea"
              fontFamily: root.serifFamily
              fontSize: 14
              enabled: root.serviceReady && !root.posting
              onClicked: root.npost.acceptPrefill()
            }

            Button {
              id: postButton
              anchors.right: parent.right
              visible: !root.pendingReplace
              text: root.posting ? "Opening…" : root.actionLabel
              selected: true
              focusable: true
              foreground: "#f4f1ea"
              fontFamily: root.serifFamily
              fontSize: 14
              enabled: root.canSubmit
              onClicked: root.submit()
            }
          }
        }
      }
    }
  }
}
