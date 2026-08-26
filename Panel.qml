import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Per-monitor presentation for the singleton composer service. Enter submits,
// Shift+Enter inserts a newline, and Escape dismisses the panel.
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
    && !npost.posting && composer.text.trim().length > 0
  readonly property int charCount: composer.text.length
  readonly property int charLimit: serviceReady ? npost.maxText : 5000
  readonly property bool pendingReplace: serviceReady && npost.pendingPrefill.length > 0

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.4)
  readonly property string family: bar ? bar.fontFamily : Style.font.family

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (opened && serviceReady) npost.refreshMode()
  }

  function submit() {
    if (serviceReady) npost.submit()
  }

  // Prefill the shared draft and open this monitor's presentation. The
  // service rejects mutation while starting or while a post is active,
  // rejects text over 5,000 characters, and will not overwrite an
  // existing draft until the user confirms.
  function compose(text) {
    var result = serviceReady ? npost.compose(text) : "service-unavailable"
    root.open()
    return result
  }


  // IPC route separate from the Panel open/close target:
  //   omarchy-shell nodoom.composer.compose compose "hello"
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
    tooltipText: "Compose on Nodoom"
    onPressed: root.toggle()

    // Preserve the supplied N mark and tint it with the bar foreground in the
    // same way the tray treats symbolic icons.
    iconComponent: Component {
      Item {
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
          source: logo
          colorization: 1.0
          colorizationColor: button.foreground
        }
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: composer
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(560))

    Column {
      id: panelColumn
      anchors.fill: parent
      spacing: Style.spacing.controlGap

      Item {
        width: parent.width
        height: headerLabel.implicitHeight

        Text {
          id: headerLabel
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: "New post"
          color: root.fg
          font.family: root.family
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }

        Text {
          anchors.left: headerLabel.right
          anchors.leftMargin: Style.spacing.controlGap
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          horizontalAlignment: Text.AlignRight
          elide: Text.ElideRight
          text: root.modeText
          color: root.serviceReady ? root.dim : Color.urgent
          font.family: root.family
          font.pixelSize: Style.font.caption
        }
      }

      TextArea {
        id: composer
        width: parent.width
        height: Style.space(120)
        text: root.serviceReady ? root.npost.draft : ""
        enabled: root.serviceReady
        readOnly: !root.serviceReady || !root.npost.ready || root.posting
        wrapMode: TextEdit.Wrap
        placeholderText: "What's on your mind?"
        color: root.fg
        selectionColor: Style.selectionFillFor(root.fg, Color.accent)
        selectedTextColor: root.fg
        placeholderTextColor: root.dim
        font.family: root.family
        font.pixelSize: Style.font.body
        leftPadding: Style.spacing.controlPaddingX + Border.left(composerBorder)
        rightPadding: Style.spacing.controlPaddingX + Border.right(composerBorder)
        topPadding: Style.spacing.inputPaddingY + Border.top(composerBorder)
        bottomPadding: Style.spacing.inputPaddingY + Border.bottom(composerBorder)
        selectByMouse: true
        Accessible.name: "Post text"

        readonly property var composerBorder: Border.controlSpec(
          activeFocus ? "focus" : (hovered ? "hover-cursor" : "normal"),
          root.fg, Color.accent)

        background: BorderSurface {
          color: Style.controlFill(composer.activeFocus, composer.hovered,
            root.fg, Color.accent)
          borderSpec: composer.composerBorder
          radius: Style.cornerRadius
        }

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
          root.close()
        }
        Keys.onReturnPressed: function(event) {
          if (event.modifiers & Qt.ShiftModifier) return
          event.accepted = true
          root.submit()
        }
        Keys.onEnterPressed: function(event) {
          if (event.modifiers & Qt.ShiftModifier) return
          event.accepted = true
          root.submit()
        }
      }

      Item {
        width: parent.width
        height: postButton.implicitHeight

        Text {
          anchors.left: parent.left
          anchors.right: root.pendingReplace ? keepButton.left : postButton.left
          anchors.rightMargin: Style.spacing.controlGap
          anchors.verticalCenter: parent.verticalCenter
          elide: Text.ElideRight
          text: root.statusText !== ""
            ? root.statusText
            : (root.charCount + " / " + root.charLimit)
          color: root.statusError ? Color.urgent : root.dim
          font.family: root.family
          font.pixelSize: Style.font.caption
        }

        Button {
          id: keepButton
          anchors.right: replaceButton.left
          anchors.rightMargin: Style.spacing.controlGap
          visible: root.pendingReplace
          text: "Keep"
          focusable: true
          foreground: root.fg
          fontFamily: root.family
          fontSize: Style.font.body
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
          foreground: root.fg
          fontFamily: root.family
          fontSize: Style.font.body
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
          foreground: root.fg
          fontFamily: root.family
          fontSize: Style.font.body
          enabled: root.canSubmit
          onClicked: root.submit()
        }
      }
    }
  }
}
