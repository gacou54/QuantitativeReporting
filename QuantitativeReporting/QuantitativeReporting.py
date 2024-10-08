from __future__ import absolute_import
from __future__ import print_function

import json
import logging
import os

import ctk
import pydicom
import qt
import slicer
import vtkSegmentationCorePython as vtkSegmentationCore
from DICOMSegmentationPlugin import DICOMSegmentationExporter
from QRCustomizations.CustomSegmentEditor import CustomSegmentEditorWidget
from QRCustomizations.CustomSegmentStatistics import CustomSegmentStatisticsParameterEditorDialog
from QRCustomizations.SegmentEditorAlgorithmTracker import SegmentEditorAlgorithmTracker
from QRUtils.htmlReport import HTMLReportCreator
from QRUtils.testdata import TestDataLogic
from SlicerDevelopmentToolboxUtils.buttons import CrosshairButton
from SlicerDevelopmentToolboxUtils.buttons import RedSliceLayoutButton, FourUpLayoutButton, FourUpTableViewLayoutButton
from SlicerDevelopmentToolboxUtils.constants import DICOMTAGS
from SlicerDevelopmentToolboxUtils.decorators import postCall
from SlicerDevelopmentToolboxUtils.forms.FormsDialog import FormsDialog
from SlicerDevelopmentToolboxUtils.helpers import WatchBoxAttribute
from SlicerDevelopmentToolboxUtils.mixins import ModuleWidgetMixin, ModuleLogicMixin
from SlicerDevelopmentToolboxUtils.widgets import CopySegmentBetweenSegmentationsWidget, TextInformationRequestDialog
from SlicerDevelopmentToolboxUtils.widgets import DICOMBasedInformationWatchBox, ImportLabelMapIntoSegmentationWidget
from slicer.ScriptedLoadableModule import *


class QuantitativeReporting(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "Quantitative Reporting"
        self.parent.categories = ["Informatics", "Quantification", "Segmentation"]
        self.parent.dependencies = ["SlicerDevelopmentToolbox"]
        self.parent.contributors = ["Christian Herz (SPL, BWH), Andrey Fedorov (SPL, BWH), "
                                    "Csaba Pinter (Queen's), Andras Lasso (Queen's), Steve Pieper (Isomics)"]
        self.parent.helpText = """
    Segmentation-based measurements with DICOM-based import and export of the results.
    <a href="https://qiicr.gitbooks.io/quantitativereporting-guide">Documentation.</a>
    """
        self.parent.acknowledgementText = """
    This work was supported in part by the National Cancer Institute funding to the
    Quantitative Image Informatics for Cancer Research (QIICR) (U24 CA180918).
    """


class QuantitativeReportingWidget(ModuleWidgetMixin, ScriptedLoadableModuleWidget):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.slicerTempDir = slicer.util.tempDirectory()
        slicer.mrmlScene.AddObserver(slicer.mrmlScene.EndCloseEvent, self.onSceneClosed)
        self.modulePath = os.path.dirname(slicer.util.modulePath(self.moduleName))

        characteristics_config_path = os.path.join(self.modulePath, 'Resources', 'Configuration', 'characteristics.json')
        with open(characteristics_config_path) as file:
            self.characteristics_config = json.load(file)

        self.delayedAutoUpdateTimer = self.createTimer(
            500,
            self.updateMeasurementsTableAndSegmentationCharacteristics,
            singleShot=True
        )

        self.segment_characteristics = {}
        # This is used for the refresh of segmentation into the characteristics group layout
        self.last_segments_position_in_characteristics_group = []

    def __del__(self):
        self.delayedAutoUpdateTimer.stop()

    def initializeMembers(self):
        self.tableNode = None
        self.segmentationObservers = []
        self.dicomSegmentationExporter = None
        self.segmentStatisticsParameterEditorDialog = None

    def enter(self):
        self._checkUserInformation()
        if self.measurementReportSelector.currentNode():
            self._useOrCreateSegmentationNodeAndConfigure()
        self.segmentEditorWidget.editor.masterVolumeNodeChanged.connect(self.onImageVolumeSelected)
        self.segmentEditorWidget.editor.segmentationNodeChanged.connect(self.onSegmentationSelected)
        qt.QTimer.singleShot(0, lambda: self.updateSizes(self.tabWidget.currentIndex))

    def exit(self):
        self.removeSegmentationObserver()
        self.segmentEditorWidget.editor.masterVolumeNodeChanged.disconnect(self.onImageVolumeSelected)
        self.segmentEditorWidget.editor.segmentationNodeChanged.disconnect(self.onSegmentationSelected)
        # self.removeDICOMBrowser()
        qt.QTimer.singleShot(0, lambda: self.tabWidget.setCurrentIndex(0))

    def _checkUserInformation(self):
        if not slicer.app.applicationLogic().GetUserInformation().GetName():
            if slicer.util.confirmYesNoDisplay("Slicer user name required to save measurement reports. \n\n"
                                               "Do you want to set it now?"):
                dialog = TextInformationRequestDialog("User Name:")
                if dialog.exec_():
                    slicer.app.applicationLogic().GetUserInformation().SetName(dialog.getValue())

    def onReload(self):
        self.cleanupUIElements()
        self.removeAllUIElements()
        super(QuantitativeReportingWidget, self).onReload()

    def onSceneClosed(self, caller, event):
        if self.measurementReportSelector.currentNode():
            self.measurementReportSelector.setCurrentNode(None)
        if hasattr(self, "watchBox"):
            self.watchBox.reset()
        if hasattr(self, "testArea"):
            self.retrieveTestDataButton.enabled = True

    def cleanupUIElements(self):
        self.removeSegmentationObserver()
        self.removeConnections()
        self.initializeMembers()

    def removeAllUIElements(self):
        for child in [c for c in self.parent.children() if not isinstance(c, qt.QVBoxLayout)]:
            try:
                child.delete()
            except AttributeError:
                pass

    def refreshUIElementsAvailability(self):

        def refresh():
            self.segmentEditorWidget.editor.masterVolumeNodeSelectorVisible = \
                self.measurementReportSelector.currentNode() and \
                not ModuleLogicMixin.getReferencedVolumeFromSegmentationNode(self.segmentEditorWidget.segmentationNode)
            masterVolume = self.segmentEditorWidget.masterVolumeNode
            self.importSegmentationCollapsibleButton.enabled = masterVolume is not None
            if not self.importSegmentationCollapsibleButton.collapsed:
                self.importSegmentationCollapsibleButton.collapsed = masterVolume is None

            self.importLabelMapCollapsibleButton.enabled = masterVolume is not None
            if not self.importLabelMapCollapsibleButton.collapsed:
                self.importLabelMapCollapsibleButton.collapsed = masterVolume is None
            if not self.tableNode:
                self.enableReportButtons(False)
                self.updateMeasurementsTable(triggered=True)

        qt.QTimer.singleShot(0, refresh)

    @postCall(refreshUIElementsAvailability)
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        self.initializeMembers()
        self.setupTabBarNavigation()
        self.setupWatchBox()
        self.setupViewSettingsArea()
        self.setupTestArea()
        self.setupSegmentationsArea()
        self.setupSelectionArea()
        self.setupImportArea()
        self.mainModuleWidgetLayout.addWidget(self.segmentationGroupBox)
        self.setupSegmentsCharacteristics()
        self.setupMeasurementsArea()
        self.setupActionButtons()

        self.setupConnections()
        self.layout.addStretch(1)
        self.fourUpSliceLayoutButton.checked = True

    def setupTabBarNavigation(self):
        self.tabWidget = qt.QTabWidget()
        self.layout.addWidget(self.tabWidget)

        self.mainModuleWidget = qt.QWidget()

        self.mainModuleWidgetLayout = qt.QGridLayout()

        self.mainModuleWidget.setLayout(self.mainModuleWidgetLayout)

        self.tabWidget.setIconSize(qt.QSize(85, 30))
        self.tabWidget.addTab(self.mainModuleWidget, 'QR')

    def enableReportButtons(self, enabled):
        self.saveReportButton.enabled = enabled
        self.completeReportButton.enabled = enabled
        self.exportToHTMLButton.enabled = enabled

    def setupWatchBox(self):
        self.watchBoxInformation = [
            WatchBoxAttribute('StudyID', 'Study ID: ', DICOMTAGS.STUDY_ID),
            WatchBoxAttribute('PatientName', 'Patient Name: ', DICOMTAGS.PATIENT_NAME),
            WatchBoxAttribute('DOB', 'Date of Birth: ', DICOMTAGS.PATIENT_BIRTH_DATE),
            WatchBoxAttribute('Reader', 'Reader Name: ',
                              callback=slicer.app.applicationLogic().GetUserInformation().GetName)]
        self.watchBox = DICOMBasedInformationWatchBox(self.watchBoxInformation)
        self.mainModuleWidgetLayout.addWidget(self.watchBox)

    def setupTestArea(self):
        self.testArea = qt.QGroupBox("Test Area")
        self.testAreaLayout = qt.QFormLayout(self.testArea)
        self.retrieveTestDataButton = self.createButton("Retrieve and load test data")
        self.testAreaLayout.addWidget(self.retrieveTestDataButton)

        if self.developerMode:
            self.mainModuleWidgetLayout.addWidget(self.testArea)

    def loadTestData(self, collection="MRHead",
                     imageDataType='volume',
                     uid="2.16.840.1.113662.4.4168496325.1025306170.548651188813145058"):
        if not len(slicer.dicomDatabase.filesForSeries(uid)):
            sampleData = TestDataLogic.downloadAndUnzipSampleData(collection)
            TestDataLogic.importIntoDICOMDatabase(sampleData[imageDataType])
        self.loadSeries(uid)
        loadedVolumeNodes = slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')
        if not loadedVolumeNodes:
            logging.error("No volumes were loaded into Slicer. Canceling.")
            return
        masterNode = loadedVolumeNodes[-1]
        tableNode = slicer.vtkMRMLTableNode()
        tableNode.SetAttribute("QuantitativeReporting", "Yes")
        slicer.mrmlScene.AddNode(tableNode)
        self.measurementReportSelector.setCurrentNode(tableNode)
        self.segmentEditorWidget.editor.setMasterVolumeNode(masterNode)
        self.retrieveTestDataButton.enabled = False

    def loadSeriesByFileName(self, filename):
        seriesUID = slicer.dicomDatabase.seriesForFile(filename)
        self.loadSeries(seriesUID)

    def loadSeries(self, seriesUID):
        from DICOMLib.DICOMUtils import loadSeriesByUID
        loadSeriesByUID([seriesUID])

    def setupSelectionArea(self):
        self.segmentEditorWidget.editor.masterVolumeNodeSelectorAddAttribute("vtkMRMLScalarVolumeNode",
                                                                             "DICOM.instanceUIDs", None)
        self.measurementReportSelector = self.createComboBox(nodeTypes=["vtkMRMLTableNode", ""],
                                                             showChildNodeTypes=False,
                                                             addEnabled=True, removeEnabled=True, noneEnabled=True,
                                                             selectNodeUponCreation=True,
                                                             toolTip="Select measurement report")
        self.measurementReportSelector.addAttribute("vtkMRMLTableNode", "QuantitativeReporting", "Yes")

        self.selectionAreaWidget = qt.QWidget()
        self.selectionAreaWidgetLayout = qt.QGridLayout()
        self.selectionAreaWidget.setLayout(self.selectionAreaWidgetLayout)

        self.selectionAreaWidgetLayout.addWidget(qt.QLabel("Measurement report"), 0, 0)
        self.selectionAreaWidgetLayout.addWidget(self.measurementReportSelector, 0, 1)
        self.mainModuleWidgetLayout.addWidget(self.selectionAreaWidget)

    def setupImportArea(self):
        self.setupImportSegmentation()
        self.setupImportLabelmap()

    def setupImportSegmentation(self):
        self.importSegmentationCollapsibleButton = ctk.ctkCollapsibleButton()
        self.importSegmentationCollapsibleButton.collapsed = True
        self.importSegmentationCollapsibleButton.enabled = False
        self.importSegmentationCollapsibleButton.text = "Import from segmentation"
        self.importSegmentsCollapsibleLayout = qt.QGridLayout(self.importSegmentationCollapsibleButton)

        self.segmentImportWidget = CopySegmentBetweenSegmentationsWidget()
        self.segmentImportWidget.addEventObserver(self.segmentImportWidget.FailedEvent, self.onImportFailed)
        self.segmentImportWidget.addEventObserver(self.segmentImportWidget.SuccessEvent, self.onImportFinished)
        self.segmentImportWidget.segmentationNodeSelectorEnabled = False
        self.importSegmentsCollapsibleLayout.addWidget(self.segmentImportWidget)
        self.mainModuleWidgetLayout.addWidget(self.importSegmentationCollapsibleButton)

    def setupImportLabelmap(self):
        self.importLabelMapCollapsibleButton = ctk.ctkCollapsibleButton()
        self.importLabelMapCollapsibleButton.collapsed = True
        self.importLabelMapCollapsibleButton.enabled = False
        self.importLabelMapCollapsibleButton.text = "Import from labelmap"
        self.importLabelMapCollapsibleLayout = qt.QGridLayout(self.importLabelMapCollapsibleButton)

        self.labelMapImportWidget = ImportLabelMapIntoSegmentationWidget()
        self.labelMapImportWidget.addEventObserver(self.labelMapImportWidget.FailedEvent, self.onImportFailed)
        self.labelMapImportWidget.addEventObserver(self.labelMapImportWidget.SuccessEvent,
                                                   self.onLabelMapImportSuccessful)
        self.labelMapImportWidget.segmentationNodeSelectorVisible = False
        self.importLabelMapCollapsibleLayout.addWidget(self.labelMapImportWidget)
        self.mainModuleWidgetLayout.addWidget(self.importLabelMapCollapsibleButton)

    def onImportFailed(self, caller, event):
        slicer.util.errorDisplay("Import failed. Check console for details.")

    def onImportFinished(self, caller, event):
        self.onSegmentationNodeChanged()

    def onLabelMapImportSuccessful(self, caller, event):
        self.hideAllLabels()

    def setupViewSettingsArea(self):
        self.redSliceLayoutButton = RedSliceLayoutButton()
        self.fourUpSliceLayoutButton = FourUpLayoutButton()
        self.fourUpSliceTableViewLayoutButton = FourUpTableViewLayoutButton()
        self.crosshairButton = CrosshairButton()
        self.crosshairButton.setSliceIntersectionEnabled(True)

        hbox = self.createHLayout([self.redSliceLayoutButton, self.fourUpSliceLayoutButton,
                                   self.fourUpSliceTableViewLayoutButton, self.crosshairButton])
        self.mainModuleWidgetLayout.addWidget(hbox)

    def setupSegmentationsArea(self):
        self.segmentationGroupBox = qt.QGroupBox("Segmentations")
        self.segmentationGroupBoxLayout = qt.QFormLayout()
        self.segmentationGroupBox.setLayout(self.segmentationGroupBoxLayout)
        self.segmentEditorWidget = CustomSegmentEditorWidget(parent=self.segmentationGroupBox)
        self.segmentEditorWidget.setup()
        self.segmentEditorAlgorithmTracker = SegmentEditorAlgorithmTracker()
        self.segmentEditorAlgorithmTracker.setSegmentEditorWidget(self.segmentEditorWidget)

    def setupSegmentsCharacteristics(self):
        self.characteristicsGroupBox = qt.QGroupBox("Characteristics")
        self.characteristicsGroupBox.setLayout(qt.QGridLayout())

        label_segment = qt.QLabel('Segment')
        label_characteristics = qt.QLabel('Characteristics')

        self.characteristicsGroupBox.layout().addWidget(label_segment, 0, 0)
        self.characteristicsGroupBox.layout().addWidget(label_characteristics, 0, 1)

        self.mainModuleWidgetLayout.addWidget(self.characteristicsGroupBox)

    def setupMeasurementsArea(self):
        self.measurementsGroupBox = qt.QGroupBox("Measurements")
        self.measurementsGroupBox.setLayout(qt.QGridLayout())
        self.tableView = slicer.qMRMLTableView()
        self.tableView.setMinimumHeight(150)
        self.tableView.setMaximumHeight(150)
        self.tableView.setSelectionBehavior(qt.QTableView.SelectRows)

        if ModuleWidgetMixin.isQtVersionOlder():
            self.tableView.horizontalHeader().setResizeMode(qt.QHeaderView.Stretch)
        else:
            self.tableView.horizontalHeader().setSectionResizeMode(qt.QHeaderView.Stretch)

        self.fourUpTableView = None
        self.segmentStatisticsConfigButton = self.createButton("Segment Statistics Parameters")

        self.calculateMeasurementsButton = self.createButton("Calculate Measurements", enabled=False)
        self.calculateAutomaticallyCheckbox = qt.QCheckBox("Auto Update")
        self.calculateAutomaticallyCheckbox.checked = True

        self.measurementsGroupBox.layout().addWidget(self.tableView, 0, 0, 1, 2)
        self.measurementsGroupBox.layout().addWidget(self.segmentStatisticsConfigButton, 1, 0, 1, 2)
        self.measurementsGroupBox.layout().addWidget(self.calculateMeasurementsButton, 2, 0)
        self.measurementsGroupBox.layout().addWidget(self.calculateAutomaticallyCheckbox, 2, 1)

        self.mainModuleWidgetLayout.addWidget(self.measurementsGroupBox)

    def setupActionButtons(self):
        self.saveReportButton = self.createButton("Save Report")
        self.completeReportButton = self.createButton("Complete Report")
        self.exportToHTMLButton = self.createButton("Export to HTML")
        self.enableReportButtons(False)
        self.mainModuleWidgetLayout.addWidget(self.createHLayout([self.saveReportButton, self.completeReportButton,
                                                                  self.exportToHTMLButton]))

    def setupConnections(self, funcName="connect"):

        def setupSelectorConnections():
            getattr(self.measurementReportSelector, funcName)('currentNodeChanged(vtkMRMLNode*)',
                                                              self.onMeasurementReportSelected)

        def setupButtonConnections():
            getattr(self.saveReportButton.clicked, funcName)(self.onSaveReportButtonClicked)
            getattr(self.completeReportButton.clicked, funcName)(self.onCompleteReportButtonClicked)
            getattr(self.calculateMeasurementsButton.clicked, funcName)(
                lambda: self.updateMeasurementsTable(triggered=True))
            getattr(self.segmentStatisticsConfigButton.clicked, funcName)(self.onEditParameters)
            getattr(self.exportToHTMLButton.clicked, funcName)(self.onExportToHTMLButtonClicked)
            getattr(self.retrieveTestDataButton.clicked, funcName)(lambda clicked: self.loadTestData())

        def setupOtherConnections():
            getattr(self.layoutManager.layoutChanged, funcName)(self.onLayoutChanged)
            getattr(self.layoutManager.layoutChanged, funcName)(self.setupFourUpTableViewConnection)
            getattr(self.calculateAutomaticallyCheckbox.toggled, funcName)(self.onCalcAutomaticallyToggled)
            getattr(self.tableView.selectionModel().selectionChanged, funcName)(self.onSegmentSelectionChanged)
            getattr(self.tabWidget.currentChanged, funcName)(self.onTabWidgetClicked)

        setupSelectorConnections()
        setupButtonConnections()
        setupOtherConnections()

    def onEditParameters(self, calculatorName=None):
        """Open dialog box to edit calculator's parameters"""
        segmentStatisticsLogic = self.segmentEditorWidget.logic.segmentStatisticsLogic
        if not self.segmentStatisticsParameterEditorDialog:
            self.segmentStatisticsParameterEditorDialog = CustomSegmentStatisticsParameterEditorDialog(
                segmentStatisticsLogic)
        self.segmentStatisticsParameterEditorDialog.exec_()
        self.updateMeasurementsTable(triggered=True)

    def onExportToHTMLButtonClicked(self):
        creator = HTMLReportCreator(self.segmentEditorWidget.segmentationNode, self.tableNode)
        creator.generateReport()

    def onTabWidgetClicked(self, currentIndex):
        if currentIndex == 0:
            slicer.app.layoutManager().parent().parent().show()
            self.dicomBrowser.close()
        elif currentIndex == 1:
            slicer.app.layoutManager().parent().parent().hide()
            self.dicomBrowser.open()

        qt.QTimer.singleShot(0, lambda: self.updateSizes(currentIndex))

    def updateSizes(self, index):
        mainWindow = slicer.util.mainWindow()
        dockWidget = slicer.util.findChildren(mainWindow, name='dockWidgetContents')[0]
        tempPolicy = dockWidget.sizePolicy
        if index == 0:
            dockWidget.setSizePolicy(qt.QSizePolicy.Maximum, qt.QSizePolicy.Preferred)
        qt.QTimer.singleShot(0, lambda: dockWidget.setSizePolicy(tempPolicy))

    def open_characteristic_window(self, segment_index):
        dialog = CharacteristicsWindow(self.segment_characteristics[segment_index], self.characteristics_config)
        dialog.exec()

    def onSegmentSelectionChanged(self, itemSelection):
        selectedRow = itemSelection.indexes()[0].row() if len(itemSelection.indexes()) else None
        if selectedRow is not None:
            self.onSegmentSelected(selectedRow)

    def onSegmentSelected(self, index):
        segmentID = self.segmentEditorWidget.getSegmentIDByIndex(index)
        self.segmentEditorWidget.editor.setCurrentSegmentID(segmentID)
        self.selectRowIfNotSelected(self.tableView, index)
        self.selectRowIfNotSelected(self.fourUpTableView, index)
        self.segmentEditorWidget.onSegmentSelected(index)

    def selectRowIfNotSelected(self, tableView, selectedRow):
        if tableView:
            if len(tableView.selectedIndexes()):
                if tableView.selectedIndexes()[0].row() != selectedRow:
                    tableView.selectRow(selectedRow)
            elif tableView.model().rowCount() > selectedRow:
                tableView.selectRow(selectedRow)

    def removeConnections(self):
        self.setupConnections(funcName="disconnect")
        if self.fourUpTableView:
            self.fourUpTableView.selectionModel().selectionChanged.disconnect(self.onSegmentSelectionChanged)

    def onCalcAutomaticallyToggled(self, checked):
        if checked and self.segmentEditorWidget.segmentation is not None:
            self.updateMeasurementsTable(triggered=True)
        self.calculateMeasurementsButton.enabled = not checked and self.tableNode

    def removeSegmentationObserver(self):
        if self.segmentEditorWidget.segmentation and len(self.segmentationObservers):
            while len(self.segmentationObservers):
                observer = self.segmentationObservers.pop()
                self.segmentEditorWidget.segmentation.RemoveObserver(observer)

    def setupFourUpTableViewConnection(self):
        if not self.fourUpTableView and self.layoutManager.layout == self.fourUpSliceTableViewLayoutButton.LAYOUT:
            if slicer.app.layoutManager().tableWidget(0):
                self.fourUpTableView = slicer.app.layoutManager().tableWidget(0).tableView()
                self.fourUpTableView.selectionModel().selectionChanged.connect(self.onSegmentSelectionChanged)
                self.fourUpTableView.setSelectionBehavior(qt.QTableView.SelectRows)

    def onLoadingFinishedEvent(self, caller, event):
        self.tabWidget.setCurrentIndex(0)

    def onLayoutChanged(self):
        self.onDisplayMeasurementsTable()

    @postCall(refreshUIElementsAvailability)
    def onSegmentationSelected(self, node):
        if not node:
            return
        masterVolume = ModuleLogicMixin.getReferencedVolumeFromSegmentationNode(node)
        if masterVolume:
            self.initializeWatchBox(masterVolume)

    @postCall(refreshUIElementsAvailability)
    def onImageVolumeSelected(self, node):
        self.seriesNumber = None
        self.initializeWatchBox(node)

    @postCall(refreshUIElementsAvailability)
    def onMeasurementReportSelected(self, node):
        # TODO check here if it's longitudinal data
        self.removeSegmentationObserver()
        self.segmentEditorWidget.editor.setMasterVolumeNode(None)
        self.calculateAutomaticallyCheckbox.checked = True
        self.tableNode = node
        # self.hideAllSegmentations()
        if node is None:
            self.segmentEditorWidget.editor.setSegmentationNode(None)
            self.updateImportArea(None)
            self.watchBox.reset()
            return

        self._useOrCreateSegmentationNodeAndConfigure()

    def _configureReadWriteAccess(self):
        if not self.tableNode:
            return
        if self.tableNode.GetAttribute("readonly"):
            logging.debug("Selected measurements report is readonly")
            self.setMeasurementsTable(self.tableNode)
            self.segmentEditorWidget.enabled = False
            self.enableReportButtons(False)
            self.calculateAutomaticallyCheckbox.enabled = False
            self.segmentStatisticsConfigButton.enabled = False
        else:
            self.segmentEditorWidget.enabled = True
            self.calculateAutomaticallyCheckbox.enabled = True
            self.segmentStatisticsConfigButton.enabled = True
            self.onSegmentationNodeChanged()
        self.exportToHTMLButton.enabled = True

    def _useOrCreateSegmentationNodeAndConfigure(self):
        segmentationNodeID = self.tableNode.GetAttribute('ReferencedSegmentationNodeID')
        logging.debug("ReferencedSegmentationNodeID {}".format(segmentationNodeID))
        if segmentationNodeID:
            segmentationNode = slicer.mrmlScene.GetNodeByID(segmentationNodeID)
        else:
            segmentationNode = self._createAndReferenceNewSegmentationNode()
        self._configureSegmentationNode(segmentationNode)
        self.updateImportArea(segmentationNode)
        self._setupSegmentationObservers()
        self._configureReadWriteAccess()

    def _configureSegmentationNode(self, node):
        # self.hideAllSegmentations()
        self.segmentEditorWidget.editor.setSegmentationNode(node)
        node.SetDisplayVisibility(True)

    def _createAndReferenceNewSegmentationNode(self):
        segmentationNode = self.createNewSegmentationNode()
        self.tableNode.SetAttribute('ReferencedSegmentationNodeID', segmentationNode.GetID())
        return segmentationNode

    def updateImportArea(self, node):
        self.segmentImportWidget.otherSegmentationNodeSelector.setCurrentNode(None)
        self.segmentImportWidget.setSegmentationNode(node)
        self.labelMapImportWidget.setSegmentationNode(node)

    def _setupSegmentationObservers(self):
        segNode = self.segmentEditorWidget.segmentation
        if not segNode:
            return
        segmentationEvents = [vtkSegmentationCore.vtkSegmentation.SegmentAdded,
                              vtkSegmentationCore.vtkSegmentation.SegmentRemoved,
                              vtkSegmentationCore.vtkSegmentation.SegmentModified,
                              vtkSegmentationCore.vtkSegmentation.RepresentationModified]

        for event in segmentationEvents:
            self.segmentationObservers.append(segNode.AddObserver(event, self.onSegmentationNodeChanged))

    def initializeWatchBox(self, node):
        if not node:
            self.watchBox.sourceFile = None
            return
        try:
            dicomFileName = slicer.dicomDatabase.fileForInstance(node.GetAttribute("DICOM.instanceUIDs").split(" ")[0])
            self.watchBox.sourceFile = dicomFileName
        except AttributeError:
            self.watchBox.sourceFile = None
            if slicer.util.confirmYesNoDisplay(
                    "The referenced master volume from the current segmentation is not of type "
                    "DICOM. QuantitativeReporting will create a new segmentation node for the "
                    "current measurement report. You will need to select a proper DICOM master "
                    "volume in order to create a segmentation. Do you want to proceed?",
                    detailedText="In some cases a non DICOM master volume was selected from the "
                                 "SegmentEditor module itself. QuantitativeReporting currently "
                                 "does not support non DICOM master volumes."):
                self._configureSegmentationNode(self._createAndReferenceNewSegmentationNode())
                self.segmentEditorWidget.editor.setMasterVolumeNode(None)
            else:
                self.measurementReportSelector.setCurrentNode(None)

    def createNewSegmentationNode(self):
        return slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")

    @postCall(refreshUIElementsAvailability)
    def onSegmentationNodeChanged(self, observer=None, caller=None):
        if self.segmentImportWidget.busy:
            return
        self.enableReportButtons(True)
        self.tableView.setStyleSheet("QTableView{border:2px solid red;};")
        self.delayedAutoUpdateTimer.start()
        # TODO self.delayedAutoUpdateTimer.start()
        # self.updateMeasurementsTable() # instead use delayed auto update triggered above

    def updateMeasurementsTableAndSegmentationCharacteristics(self, triggered=False, visibleOnly=False):
        self.updateMeasurementsTable(triggered, visibleOnly)
        self.updateSegmentationCharacteristics()

    def updateSegmentationCharacteristics(self):
        segments = self.segmentEditorWidget.logic.getVisibleSegments(self.segmentEditorWidget.segmentationNode)

        # To update correctly, remove the last widget and add new ones
        # We can do this since the widget are not link to characteristics.
        # The segment IDs are link to the characteristics.
        for i in self.last_segments_position_in_characteristics_group:
            self.removeWidgetAtPositionInCharacteristicGroup(i, 0)
            self.removeWidgetAtPositionInCharacteristicGroup(i, 1)

        self.last_segments_position_in_characteristics_group = []

        # Remove characteristics from the segment_characteristics when a segment have been deleted.
        # This for loop looks if there is a segment in segment_characteristics that is not in the
        # segment editor.
        segment_ids = [s.GetLabelValue() for s in segments]
        new_segment_characteristics = {s: c for s, c in self.segment_characteristics.items() if s in segment_ids}
        self.segment_characteristics = new_segment_characteristics

        for i, segment in enumerate(segments):
            segment_label = qt.QLabel(segment.GetName())
            button = qt.QPushButton('Add characteristics')

            segmentID = segment.GetLabelValue()
            if segmentID not in self.segment_characteristics:
                self.segment_characteristics[segmentID] = {}

            # This is weird, but it works (https://stackoverflow.com/a/57167056)
            button.clicked.connect(lambda _, id_=segmentID: self.open_characteristic_window(id_))

            position_in_characteristics_group = i + 1
            self.characteristicsGroupBox.layout().addWidget(segment_label, position_in_characteristics_group, 0)
            self.characteristicsGroupBox.layout().addWidget(button, position_in_characteristics_group, 1)
            self.last_segments_position_in_characteristics_group.append(position_in_characteristics_group)

    def removeWidgetAtPositionInCharacteristicGroup(self, row, column):
        layout = self.characteristicsGroupBox.layout()
        item = layout.itemAtPosition(row, column)

        if item is not None:
            widget = item.widget()
            if widget is not None:
                # Remove the widget from the layout
                layout.removeWidget(widget)
                # Optionally delete the widget
                widget.deleteLater()

    def updateMeasurementsTable(self, triggered=False, visibleOnly=False):
        if not self.calculateAutomaticallyCheckbox.checked and not triggered:
            self.tableView.setStyleSheet("QTableView{border:2px solid red;};")
            return
        table = self.segmentEditorWidget.calculateSegmentStatistics(self.tableNode, visibleOnly)
        self.setMeasurementsTable(table)

    def setMeasurementsTable(self, table):
        if table:
            self.tableNode = table
            self.tableNode.SetLocked(True)
            self.tableView.setMRMLTableNode(self.tableNode)
            self.tableView.setStyleSheet("QTableView{border:none};")
        else:
            if self.tableNode:
                self.tableNode.RemoveAllColumns()
            self.tableView.setMRMLTableNode(self.tableNode if self.tableNode else None)
        self.onDisplayMeasurementsTable()

    def onDisplayMeasurementsTable(self):
        self.tableView.visible = not self.layoutManager.layout == self.fourUpSliceTableViewLayoutButton.LAYOUT
        if self.layoutManager.layout == self.fourUpSliceTableViewLayoutButton.LAYOUT and self.tableNode:
            slicer.app.applicationLogic().GetSelectionNode().SetReferenceActiveTableID(self.tableNode.GetID())
            slicer.app.applicationLogic().PropagateTableSelection()

    def onSaveReportButtonClicked(self):
        success, err = self.saveReport()
        self.saveReportButton.enabled = not success
        if success:
            slicer.util.infoDisplay("Report successfully saved into SlicerDICOMDatabase")
        if err:
            slicer.util.warningDisplay(err)

    def onCompleteReportButtonClicked(self):
        success, err = self.saveReport(completed=True)
        self.saveReportButton.enabled = not success
        self.completeReportButton.enabled = not success
        if success:
            slicer.util.infoDisplay("Report successfully completed and saved into SlicerDICOMDatabase")
            self.tableNode.SetAttribute("readonly", "Yes")
        else:
            slicer.util.warningDisplay(err)

    def saveReport(self, completed=False):

        self._metadata = self.retrieveMetaDataFromUser()
        if not self._metadata:
            return False, "Saving process canceled. Meta-information was not confirmed by user."
        try:
            dcmSegPath = self.createSEG()
            dcmSRPath = self.createDICOMSR(dcmSegPath, completed)
            if dcmSegPath and dcmSRPath:
                indexer = ctk.ctkDICOMIndexer()
                indexer.addFile(slicer.dicomDatabase, dcmSegPath, "copy")
                indexer.addFile(slicer.dicomDatabase, dcmSRPath, "copy")
        except (RuntimeError, ValueError, AttributeError) as exc:
            return False, exc.args
        finally:
            self.cleanupTemporaryData()
        return True, None

    def retrieveMetaDataFromUser(self):
        settings = qt.QSettings()
        settings.beginGroup("QuantitativeReporting/GeneralContentInformationDefaults")
        schema = os.path.join(self.modulePath, 'Resources', 'Validation', 'general_content_schema.json')
        metaDataFormWidget = FormsDialog([schema], defaultSettings=settings)
        settings.endGroup()

        metadata = None
        if metaDataFormWidget.exec_():
            metadata = metaDataFormWidget.getData()
            self._persistEnteredMetaData(metadata)
        return metadata

    def _persistEnteredMetaData(self, metadata):
        settings = qt.QSettings()
        settings.beginGroup("QuantitativeReporting/GeneralContentInformationDefaults")
        for attr in list(metadata.keys()):
            settings.setValue(attr, metadata[attr])
        settings.endGroup()

    def createSEG(self):
        self.dicomSegmentationExporter = DICOMSegmentationExporter(self.segmentEditorWidget.segmentationNode)
        segFilename = "quantitative_reporting_export.SEG" + self.dicomSegmentationExporter.currentDateTime + ".dcm"
        dcmSegmentationPath = os.path.join(self.dicomSegmentationExporter.tempDir, segFilename)
        segmentIDs = None
        if self.segmentEditorWidget.hiddenSegmentsAvailable():
            if not slicer.util.confirmYesNoDisplay(
                    "Hidden segments have been found. Do you want to export them as well?"):
                self.updateMeasurementsTable(visibleOnly=True)
                visibleSegments = self.segmentEditorWidget.logic.getVisibleSegments(
                    self.segmentEditorWidget.segmentationNode)
                segmentIDs = [segment.GetName() for segment in visibleSegments]
        try:
            try:
                self.dicomSegmentationExporter.export(outputDirectory=os.path.dirname(dcmSegmentationPath),
                                                      segmentIDs=segmentIDs,
                                                      segFileName=os.path.basename(dcmSegmentationPath),
                                                      metadata=self._metadata)
            except DICOMSegmentationExporter.MissingAttributeError as exc:
                raise ValueError("Missing attributes: %s " % str(exc))
            except DICOMSegmentationExporter.EmptySegmentsFoundError:
                raise ValueError("Empty segments found. Please make sure that there are no empty segments.")
            logging.debug("Saved DICOM Segmentation to {}".format(dcmSegmentationPath))
            slicer.dicomDatabase.insert(dcmSegmentationPath)
            logging.info("Added segmentation to DICOM database (%s)", dcmSegmentationPath)
        except (DICOMSegmentationExporter.NoNonEmptySegmentsFoundError, ValueError) as exc:
            raise ValueError(exc.args)
        return dcmSegmentationPath

    def createDICOMSR(self, referencedSegmentation, completed):
        data = self.dicomSegmentationExporter.getSeriesAttributes()
        data["SeriesDescription"] = "Measurement Report"

        compositeContextDataDir, data["compositeContext"] = \
            os.path.dirname(referencedSegmentation), [os.path.basename(referencedSegmentation)]
        imageLibraryDataDir, data["imageLibrary"] = \
            self.dicomSegmentationExporter.getDICOMFileList(self.segmentEditorWidget.masterVolumeNode)
        data.update(self._getAdditionalSRInformation(completed))

        data["Measurements"] = \
            self.segmentEditorWidget.logic.segmentStatisticsLogic.generateJSON4DcmSR(referencedSegmentation,
                                                                                     self.segmentEditorWidget.masterVolumeNode)
        logging.debug("DICOM SR Metadata output:")
        logging.debug(json.dumps(data, indent=2, separators=(',', ': ')))

        metaFilePath = self.saveJSON(data, os.path.join(self.dicomSegmentationExporter.tempDir, "sr_meta.json"))
        outputSRPath = os.path.join(self.dicomSegmentationExporter.tempDir, "sr.dcm")

        params = {"metaDataFileName": metaFilePath,
                  "compositeContextDataDir": compositeContextDataDir,
                  "imageLibraryDataDir": imageLibraryDataDir,
                  "outputFileName": outputSRPath}

        logging.debug(params)
        cliNode = slicer.cli.run(slicer.modules.tid1500writer, None, params, wait_for_completion=True)

        if cliNode.GetStatusString() != 'Completed':
            raise Exception("tid1500writer CLI did not complete cleanly")

        # We look in the SEG dicom dataset and retrieve the segmentation.
        # The segmentation seems to be simply put in order in the DICOM file.
        seg_ds = pydicom.dcmread(referencedSegmentation)
        sorted_segment_characteristics_keys = sorted(self.segment_characteristics)
        if len(seg_ds.SegmentSequence) != len(sorted_segment_characteristics_keys):
            raise ValueError(
                f'Number of segmentation ({len(seg_ds)}) in SEG DICOM != Number of segmentation charateristics ({len(self.segment_characteristics)})'
            )

        sr_ds = pydicom.dcmread(outputSRPath)
        # In the DICOM SR TID1500, the 6 element is the place that contains segmentations additional information
        # Each dataset correspond to a segment.
        for dataset, key in zip(sr_ds.ContentSequence[5].ContentSequence, sorted_segment_characteristics_keys):
            for concept_name, choice_label in self.segment_characteristics[key].items():
                characteristics = _find_characteristics_from_concept_name_and_choice(concept_name, choice_label, self.characteristics_config)
                if characteristics is None:
                    continue

                characteristics_ds = create_dataset_from_characteristics(characteristics)
                dataset.ContentSequence.append(characteristics_ds)

        sr_ds.save_as(outputSRPath)

        return outputSRPath

    def cleanupTemporaryData(self):
        if self.dicomSegmentationExporter:
            self.dicomSegmentationExporter.cleanup()
        self.dicomSegmentationExporter = None

    def _getAdditionalSRInformation(self, completed=False):
        data = dict()
        data["observerContext"] = {"ObserverType": "PERSON",
                                   "PersonObserverName": self._metadata["ContentCreatorName"]}
        data["VerificationFlag"] = "VERIFIED" if completed else "UNVERIFIED"
        data["CompletionFlag"] = "COMPLETE" if completed else "PARTIAL"
        data["activitySession"] = "1"
        data["timePoint"] = self._metadata["ClinicalTrialTimePointID"]
        return data

    def saveJSON(self, data, destination):
        with open(os.path.join(destination), 'w') as outfile:
            json.dump(data, outfile, indent=2)
        return destination


class QuantitativeReportingSlicelet(qt.QWidget, ModuleWidgetMixin):

    def __init__(self):
        qt.QWidget.__init__(self)
        self.mainWidget = qt.QWidget()
        self.mainWidget.objectName = "qSlicerAppMainWindow"
        self.mainWidget.setLayout(qt.QHBoxLayout())

        self.setupLayoutWidget()

        self.moduleFrame = qt.QWidget()
        self.moduleFrame.setLayout(qt.QVBoxLayout())
        self.widget = QuantitativeReportingWidget(self.moduleFrame)
        self.widget.setup()

        # TODO: resize self.widget.parent to minimum possible width

        self.scrollArea = qt.QScrollArea()
        self.scrollArea.setWidget(self.widget.parent)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setMinimumWidth(self.widget.parent.minimumSizeHint.width())

        self.splitter = qt.QSplitter()
        self.splitter.setOrientation(qt.Qt.Horizontal)
        self.splitter.addWidget(self.scrollArea)
        self.splitter.addWidget(self.layoutWidget)
        self.splitter.splitterMoved.connect(self.onSplitterMoved)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.handle(1).installEventFilter(self)

        self.mainWidget.layout().addWidget(self.splitter)
        self.mainWidget.show()

    def setupLayoutWidget(self):
        self.layoutWidget = qt.QWidget()
        self.layoutWidget.setLayout(qt.QHBoxLayout())
        layoutWidget = slicer.qMRMLLayoutWidget()
        layoutManager = slicer.qSlicerLayoutManager()
        layoutManager.setMRMLScene(slicer.mrmlScene)
        layoutManager.setScriptedDisplayableManagerDirectory(
            slicer.app.slicerHome + "/bin/Python/mrmlDisplayableManager")
        layoutWidget.setLayoutManager(layoutManager)
        slicer.app.setLayoutManager(layoutManager)
        layoutWidget.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        self.layoutWidget.layout().addWidget(layoutWidget)

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.MouseButtonDblClick:
            self.onSplitterClick()

    def onSplitterMoved(self, pos, index):
        vScroll = self.scrollArea.verticalScrollBar()
        print(self.moduleFrame.width, self.widget.parent.width, self.scrollArea.width, vScroll.width)
        vScrollbarWidth = 4 if not vScroll.isVisible() else vScroll.width + 4  # TODO: find out, what is 4px wide
        if self.scrollArea.minimumWidth != self.widget.parent.minimumSizeHint.width() + vScrollbarWidth:
            self.scrollArea.setMinimumWidth(self.widget.parent.minimumSizeHint.width() + vScrollbarWidth)

    def onSplitterClick(self):
        if self.splitter.sizes()[0] > 0:
            self.splitter.setSizes([0, self.splitter.sizes()[1]])
        else:
            minimumWidth = self.widget.parent.minimumSizeHint.width()
            self.splitter.setSizes([minimumWidth, self.splitter.sizes()[1] - minimumWidth])


class CharacteristicsWindow(qt.QDialog):
    def __init__(self, segment_characteristics, characteristics_config):
        """segment_characteristics is a dict of the specific segment's characteristics
        {'characteristics_name': 'choice_value'}
        """
        super().__init__()
        self.setWindowTitle('Segment Characteristics')
        self.setGeometry(150, 150, 600, 600)

        self.characteristicsGroupBox = qt.QGroupBox("Characteristics")
        self.characteristicsGroupBox.setLayout(qt.QGridLayout())

        self.characteristic_widgets = {}
        self.segment_characteristics = segment_characteristics

        for i, characteristic in enumerate(characteristics_config):
            char_name = characteristic['ConceptNameCodeSequence']['CodeMeaning']
            characteristic_widget = qt.QComboBox()
            for choice in characteristic['choices']:
                characteristic_widget.addItem(choice['CodeMeaning'])

            label_characteristics = qt.QLabel(char_name)
            self.characteristicsGroupBox.layout().addWidget(label_characteristics, i, 0)
            self.characteristicsGroupBox.layout().addWidget(characteristic_widget, i, 1)

            if char_name in self.segment_characteristics:
                characteristic_widget.setCurrentText(self.segment_characteristics[char_name])

            self.characteristic_widgets[char_name] = characteristic_widget

        accept_button = qt.QPushButton('OK', self)
        accept_button.clicked.connect(self._set_characteristics_data_and_close)

        layout = qt.QVBoxLayout()
        layout.addWidget(self.characteristicsGroupBox)
        layout.addWidget(accept_button)
        self.setLayout(layout)

    def _set_characteristics_data_and_close(self):
        for char, widget in self.characteristic_widgets.items():
            # capture characteristics data from widgets
            self.segment_characteristics[char] = widget.currentText

        self.accept()


def _find_characteristics_from_concept_name_and_choice(concept_name, choice_label, characteristics_config):
    if choice_label == 'N/A':
        return None  # If nothing have been selected, ignore

    for i in characteristics_config:
        if i['ConceptNameCodeSequence']['CodeMeaning'] == concept_name:
            for j in i['choices']:
                if j['CodeMeaning'] == choice_label:
                    return {
                        'ConceptNameCodeSequence': i['ConceptNameCodeSequence'],
                        'ConceptCodeSequence': j
                    }

    raise ValueError(f'Concept name code (CodeMeaning={concept_name}) with choices {choice_label} not found')


def create_dataset_from_characteristics(characteristics_dict):
    ds = pydicom.Dataset()

    ds.RelationshipType = 'CONTAINS'
    ds.ValueType = 'CODE'

    concept_name_code_sequence = pydicom.Dataset()
    concept_name_code_sequence.CodeValue = characteristics_dict['ConceptNameCodeSequence']['CodeValue']
    concept_name_code_sequence.CodingSchemeDesignator = characteristics_dict['ConceptNameCodeSequence'][
        'CodingSchemeDesignator']
    concept_name_code_sequence.CodeMeaning = characteristics_dict['ConceptNameCodeSequence']['CodeMeaning']
    ds.ConceptNameCodeSequence = [concept_name_code_sequence]

    concept_code_sequence = pydicom.Dataset()
    concept_code_sequence.CodeValue = characteristics_dict['ConceptCodeSequence']['CodeValue']
    concept_code_sequence.CodingSchemeDesignator = characteristics_dict['ConceptCodeSequence']['CodingSchemeDesignator']
    concept_code_sequence.CodeMeaning = characteristics_dict['ConceptCodeSequence']['CodeMeaning']

    ds.ConceptCodeSequence = [concept_code_sequence]

    return ds


if __name__ == "QuantitativeReportingSlicelet":
    import sys

    print((sys.argv))

    slicelet = QuantitativeReportingSlicelet()
