// Copyright (c) 2020 Ultimaker B.V.
// Cura is released under the terms of the LGPLv3 or higher.

import QtQuick 2.10
import QtQuick.Controls 2.3
import QtQuick.Layouts 1.3

import UM 1.5 as UM
import Cura 1.1 as Cura

import "../Widgets"


//
// ComboBox with dropdown options in the Machine Settings dialog.
//
UM.TooltipArea
{
    id: comboBoxWithOptions

    UM.I18nCatalog { id: catalog; name: "cura"; }

    height: childrenRect.height
    width: childrenRect.width
    text: tooltipText

    property int controlWidth: UM.Theme.getSize("setting_control").width
    property int controlHeight: UM.Theme.getSize("setting_control").height

    property alias containerStackId: propertyProvider.containerStackId
    property alias settingKey: propertyProvider.key
    property alias settingStoreIndex: propertyProvider.storeIndex

    property alias labelText: fieldLabel.text
    property alias labelFont: fieldLabel.font
    property alias labelWidth: fieldLabel.width
    property alias optionModel: comboBox.model

    property string tooltipText: propertyProvider.properties.description ? propertyProvider.properties.description : ""

    // callback functions
    property var forceUpdateOnChangeFunction: dummy_func
    property var afterOnEditingFinishedFunction: dummy_func
    property var setValueFunction: null

    // a dummy function for default property values
    function dummy_func() {}

    UM.SettingPropertyProvider
    {
        id: propertyProvider
        watchedProperties: [ "value", "options", "description" ]
    }

    UM.Label
    {
        id: fieldLabel
        anchors.left: parent.left
        anchors.verticalCenter: comboBox.verticalCenter
        visible: text != ""
        font: UM.Theme.getFont("medium")
    }

    ListModel
    {
        id: defaultOptionsModel

        function updateModel()
        {
            clear()

            if(!propertyProvider.properties.options)
            {
                return
            }

            for (var i = 0; i < propertyProvider.properties["options"].keys().length; i++)
            {
                var key = propertyProvider.properties["options"].keys()[i]
                var value = propertyProvider.properties["options"][key]
                append({ text: value, code: key})

                if (propertyProvider.properties.value == key)
                {
                    currentIndex = i
                }
            }
        }

        Component.onCompleted: updateModel()
    }

    // Remake the model when the model is bound to a different container stack
    Connections
    {
        target: propertyProvider
        function onContainerStackChanged() { defaultOptionsModel.updateModel() }
        function onIsValueUsedChanged() { defaultOptionsModel.updateModel() }
    }

    Cura.ComboBox
    {
        id: comboBox
        anchors.left: fieldLabel.right
        anchors.leftMargin: UM.Theme.getSize("default_margin").width
        width: comboBoxWithOptions.controlWidth
        height: comboBoxWithOptions.controlHeight
        model: defaultOptionsModel
        textRole: "text"

        currentIndex:
        {
            var currentValue = propertyProvider.properties.value
            var index = 0
            for (var i = 0; i < model.count; i++)
            {
                if (model.get(i).value == currentValue)
                {
                    index = i
                    break
                }
            }
            return index
        }

        onActivated:
        {
            var newValue = model.get(index).value
            if (propertyProvider.properties.value != newValue)
            {
                if (setValueFunction !== null)
                {
                    setValueFunction(newValue)
                }
                else
                {
                    propertyProvider.setPropertyValue("value", newValue)
                }
                forceUpdateOnChangeFunction()
                afterOnEditingFinishedFunction()
            }
        }
    }
}
