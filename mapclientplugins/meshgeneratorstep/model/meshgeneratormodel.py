"""
Mesh generator class. Generates Zinc meshes using scaffoldmaker.
"""

from __future__ import division
import copy
import os
import math
import string

from opencmiss.utils.zinc.field import findOrCreateFieldCoordinates, findOrCreateFieldStoredMeshLocation, findOrCreateFieldStoredString
from opencmiss.utils.zinc.finiteelement import evaluateFieldNodesetRange
from opencmiss.utils.zinc.general import ChangeManager
from opencmiss.utils.maths.vectorops import axis_angle_to_rotation_matrix, euler_to_rotation_matrix, matrix_mult, rotation_matrix_to_euler
from opencmiss.zinc.field import Field, FieldGroup
from opencmiss.zinc.glyph import Glyph
from opencmiss.zinc.graphics import Graphics
from opencmiss.zinc.node import Node
from opencmiss.zinc.result import RESULT_OK, RESULT_WARNING_PART_DONE
from opencmiss.zinc.scenecoordinatesystem import SCENECOORDINATESYSTEM_WORLD
from scaffoldmaker.scaffolds import Scaffolds
from scaffoldmaker.scaffoldpackage import ScaffoldPackage
from scaffoldmaker.utils.exportvtk import ExportVtk
from scaffoldmaker.utils.zinc_utils import *

STRING_FLOAT_FORMAT = '{:.8g}'


def parseVector3(vectorText : str, delimiter, defaultValue):
    """
    Parse a 3 component vector from a string.
    Repeats last component if too few.
    :param vectorText: string containing vector components separated by delimiter.
    :param delimiter: character delimiter between component values.
    :param defaultValue: Value to use for invalid components.
    :return: list of 3 component values parsed from vectorText.
    """
    vector = []
    for valueText in vectorText.split(delimiter):
        try:
            vector.append(float(valueText))
        except:
            vector.append(defaultValue)
    if len(vector) > 3:
        vector = vector[:3]
    else:
        for i in range(3 - len(vector)):
            vector.append(vector[-1])
    return vector


class MeshGeneratorModel(object):
    """
    Framework for generating meshes of a number of types, with mesh type specific options
    """

    def __init__(self, region, material_module):
        super(MeshGeneratorModel, self).__init__()
        self._region_name = "generated_mesh"
        self._parent_region = region
        self._materialmodule = material_module
        self._region = None
        self._fieldmodulenotifier = None
        self._annotationGroups = None
        self._customParametersCallback = None
        self._sceneChangeCallback = None
        self._transformationChangeCallback = None
        self._deleteElementRanges = []
        self._nodeDerivativeLabels = [ 'D1', 'D2', 'D3', 'D12', 'D13', 'D23', 'D123' ]
        # list of nested scaffold packages to that being edited, with their parent option names
        # discover all mesh types and set the current from the default
        scaffolds = Scaffolds()
        self._allScaffoldTypes = scaffolds.getScaffoldTypes()
        scaffoldType = scaffolds.getDefaultScaffoldType()
        scaffoldPackage = ScaffoldPackage(scaffoldType)
        self._parameterSetName = scaffoldType.getParameterSetNames()[0]
        self._scaffoldPackages = [ scaffoldPackage ]
        self._scaffoldPackageOptionNames = [ None ]
        self._settings = {
            'scaffoldPackage' : scaffoldPackage,
            'deleteElementRanges' : '',
            'displayNodePoints' : False,
            'displayNodeNumbers' : False,
            'displayNodeDerivatives' : False,
            'displayNodeDerivativeLabels' : self._nodeDerivativeLabels[0:3],
            'displayLines' : True,
            'displayLinesExterior' : False,
            'displayModelRadius' : False,
            'displaySurfaces' : True,
            'displaySurfacesExterior' : True,
            'displaySurfacesTranslucent' : True,
            'displaySurfacesWireframe' : False,
            'displayElementNumbers' : False,
            'displayElementAxes' : False,
            'displayAxes' : True,
            'displayMarkerPoints' : False
        }
        self._customScaffoldPackage = None  # temporary storage of custom mesh options and edits, to switch back to
        self._unsavedNodeEdits = False  # Whether nodes have been edited since ScaffoldPackage meshEdits last updated

    def _updateMeshEdits(self):
        '''
        Ensure mesh edits are up-to-date.
        '''
        if self._unsavedNodeEdits:
            self._scaffoldPackages[-1].setMeshEdits(exnodeStringFromGroup(self._region, 'meshEdits', [ 'coordinates' ]))
            self._unsavedNodeEdits = False

    def _saveCustomScaffoldPackage(self):
        '''
        Copy current ScaffoldPackage to custom ScaffoldPackage to be able to switch back to later.
        '''
        self._updateMeshEdits()
        scaffoldPackage = self._scaffoldPackages[-1]
        self._customScaffoldPackage = ScaffoldPackage(scaffoldPackage.getScaffoldType(), scaffoldPackage.toDict())

    def _useCustomScaffoldPackage(self):
        if (not self._customScaffoldPackage) or (self._parameterSetName != 'Custom'):
            self._saveCustomScaffoldPackage()
            self._parameterSetName = 'Custom'
            if self._customParametersCallback:
                self._customParametersCallback()

    def getMeshEditsGroup(self):
        fm = self._region.getFieldmodule()
        return fm.findFieldByName('meshEdits').castGroup()

    def getOrCreateMeshEditsNodesetGroup(self, nodeset):
        '''
        Someone is about to edit a node, and must add the modified node to this nodesetGroup.
        '''
        fm = self._region.getFieldmodule()
        with ChangeManager(fm):
            group = fm.findFieldByName('meshEdits').castGroup()
            if not group.isValid():
                group = fm.createFieldGroup()
                group.setName('meshEdits')
                group.setManaged(True)
            self._unsavedNodeEdits = True
            self._useCustomScaffoldPackage()
            fieldNodeGroup = group.getFieldNodeGroup(nodeset)
            if not fieldNodeGroup.isValid():
                fieldNodeGroup = group.createFieldNodeGroup(nodeset)
            nodesetGroup = fieldNodeGroup.getNodesetGroup()
        return nodesetGroup

    def interactionRotate(self, axis, angle):
        mat1 = axis_angle_to_rotation_matrix(axis, angle)
        mat2 = euler_to_rotation_matrix([ deg*math.pi/180.0 for deg in self._scaffoldPackages[-1].getRotation() ])
        newmat = matrix_mult(mat1, mat2)
        rotation = [ rad*180.0/math.pi for rad in rotation_matrix_to_euler(newmat) ]
        if self._scaffoldPackages[-1].setRotation(rotation):
            self._setGraphicsTransformation()
            if self._transformationChangeCallback:
                self._transformationChangeCallback()

    def interactionScale(self, uniformScale):
        scale = self._scaffoldPackages[-1].getScale()
        if self._scaffoldPackages[-1].setScale([ (scale[i]*uniformScale) for i in range(3) ]):
            self._setGraphicsTransformation()
            if self._transformationChangeCallback:
                self._transformationChangeCallback()

    def interactionTranslate(self, offset):
        translation = self._scaffoldPackages[-1].getTranslation()
        if self._scaffoldPackages[-1].setTranslation([ (translation[i] + offset[i]) for i in range(3) ]):
            self._setGraphicsTransformation()
            if self._transformationChangeCallback:
                self._transformationChangeCallback()

    def interactionEnd(self):
        pass

    def _setScaffoldType(self, scaffoldType):
        if len(self._scaffoldPackages) == 1:
            # root scaffoldPackage
            self._scaffoldPackages[0].__init__(scaffoldType)
        else:
            # nested ScaffoldPackage
            self._scaffoldPackages[-1].deepcopy(self.getParentScaffoldType().getOptionScaffoldPackage(self._scaffoldPackageOptionNames[-1], scaffoldType))
        self._customScaffoldPackage = None
        self._unsavedNodeEdits = False
        self._parameterSetName = self.getEditScaffoldParameterSetNames()[0]

    def _getScaffoldTypeByName(self, name):
        for scaffoldType in self._allScaffoldTypes:
            if scaffoldType.getName() == name:
                return scaffoldType
        return None

    def setScaffoldTypeByName(self, name):
        scaffoldType = self._getScaffoldTypeByName(name)
        if scaffoldType is not None:
            parentScaffoldType = self.getParentScaffoldType()
            assert (not parentScaffoldType) or (scaffoldType in parentScaffoldType.getOptionValidScaffoldTypes(self._scaffoldPackageOptionNames[-1])), \
               'Invalid scaffold type for parent scaffold'
            if scaffoldType != self.getEditScaffoldType():
                self._setScaffoldType(scaffoldType)
                self._generateMesh()

    def getAvailableScaffoldTypeNames(self):
        scaffoldTypeNames = []
        parentScaffoldType = self.getParentScaffoldType()
        validScaffoldTypes = parentScaffoldType.getOptionValidScaffoldTypes(self._scaffoldPackageOptionNames[-1]) if parentScaffoldType else None
        for scaffoldType in self._allScaffoldTypes:
            if (not parentScaffoldType) or (scaffoldType in validScaffoldTypes):
                scaffoldTypeNames.append(scaffoldType.getName())
        return scaffoldTypeNames

    def getEditScaffoldTypeName(self):
        return self.getEditScaffoldType().getName()

    def editingRootScaffoldPackage(self):
        '''
        :return: True if editing root ScaffoldPackage, else False.
        '''
        return len(self._scaffoldPackages) == 1

    def getEditScaffoldType(self):
        '''
        Get scaffold type currently being edited, including nested scaffolds.
        '''
        return self._scaffoldPackages[-1].getScaffoldType()

    def getEditScaffoldSettings(self):
        '''
        Get settings for scaffold type currently being edited, including nested scaffolds.
        '''
        return self._scaffoldPackages[-1].getScaffoldSettings()

    def getEditScaffoldOptionDisplayName(self):
        '''
        Get option display name for sub scaffold package being edited.
        '''
        return '/'.join(self._scaffoldPackageOptionNames[1:])

    def getEditScaffoldOrderedOptionNames(self):
        return self._scaffoldPackages[-1].getScaffoldType().getOrderedOptionNames()

    def getEditScaffoldParameterSetNames(self):
        if self.editingRootScaffoldPackage():
            return self._scaffoldPackages[0].getScaffoldType().getParameterSetNames()
        # may need to change if scaffolds nested two deep
        return self.getParentScaffoldType().getOptionScaffoldTypeParameterSetNames( \
            self._scaffoldPackageOptionNames[-1], self._scaffoldPackages[-1].getScaffoldType())

    def getDefaultScaffoldPackageForParameterSetName(self, parameterSetName):
        '''
        :return: Default ScaffoldPackage set up with named parameter set.
        '''
        if self.editingRootScaffoldPackage():
            scaffoldType = self._scaffoldPackages[0].getScaffoldType()
            return ScaffoldPackage(scaffoldType, { 'scaffoldSettings' : scaffoldType.getDefaultOptions(parameterSetName) })
        # may need to change if scaffolds nested two deep
        return self.getParentScaffoldType().getOptionScaffoldPackage( \
            self._scaffoldPackageOptionNames[-1], self._scaffoldPackages[-1].getScaffoldType(), parameterSetName)

    def getEditScaffoldOption(self, key):
        return self.getEditScaffoldSettings()[key]

    def getParentScaffoldType(self):
        '''
        :return: Parent scaffold type or None if root scaffold.
        '''
        if len(self._scaffoldPackages) > 1:
            return self._scaffoldPackages[-2].getScaffoldType()
        return None

    def getParentScaffoldOption(self, key):
        assert len(self._scaffoldPackages) > 1, 'Attempt to get parent option on root scaffold'
        parentScaffoldSettings = self._scaffoldPackages[-2].getScaffoldSettings()
        return parentScaffoldSettings[key]

    def _checkCustomParameterSet(self):
        '''
        Work out whether ScaffoldPackage has a predefined parameter set or 'Custom'.
        '''
        self._customScaffoldPackage = None
        self._unsavedNodeEdits = False
        self._parameterSetName = None
        scaffoldPackage = self._scaffoldPackages[-1]
        for parameterSetName in reversed(self.getEditScaffoldParameterSetNames()):
            tmpScaffoldPackage = self.getDefaultScaffoldPackageForParameterSetName(parameterSetName)
            if tmpScaffoldPackage == scaffoldPackage:
                self._parameterSetName = parameterSetName
                break
        if not self._parameterSetName:
            self._useCustomScaffoldPackage()

    def _clearMeshEdits(self):
        self._scaffoldPackages[-1].setMeshEdits(None)
        self._unsavedNodeEdits = False

    def editScaffoldPackageOption(self, optionName):
        '''
        Switch to editing a nested scaffold.
        '''
        settings = self.getEditScaffoldSettings()
        scaffoldPackage = settings.get(optionName)
        assert isinstance(scaffoldPackage, ScaffoldPackage), 'Option is not a ScaffoldPackage'
        self._clearMeshEdits()
        self._scaffoldPackages.append(scaffoldPackage)
        self._scaffoldPackageOptionNames.append(optionName)
        self._checkCustomParameterSet()
        self._generateMesh()

    def endEditScaffoldPackageOption(self):
        '''
        End editing of the last ScaffoldPackage, moving up to parent or top scaffold type.
        '''
        assert len(self._scaffoldPackages) > 1, 'Attempt to end editing root ScaffoldPackage'
        self._updateMeshEdits()
        self._scaffoldPackages.pop()
        self._scaffoldPackageOptionNames.pop()
        self._checkCustomParameterSet()
        self._generateMesh()

    def getAvailableParameterSetNames(self):
        parameterSetNames = self.getEditScaffoldParameterSetNames()
        if self._customScaffoldPackage:
            parameterSetNames.insert(0, 'Custom')
        return parameterSetNames

    def getParameterSetName(self):
        '''
        :return: Name of currently active parameter set.
        '''
        return self._parameterSetName

    def setParameterSetName(self, parameterSetName):
        if self._parameterSetName == 'Custom':
            self._saveCustomScaffoldPackage()
        if parameterSetName == 'Custom':
            sourceScaffoldPackage = self._customScaffoldPackage
        else:
            sourceScaffoldPackage = self.getDefaultScaffoldPackageForParameterSetName(parameterSetName)
        self._scaffoldPackages[-1].deepcopy(sourceScaffoldPackage)
        self._parameterSetName = parameterSetName
        self._unsavedNodeEdits = False
        self._generateMesh()

    def setScaffoldOption(self, key, value):
        '''
        :return: True if other dependent options have changed, otherwise False.
        On True return client is expected to refresh all option values in UI.
        '''
        scaffoldType = self.getEditScaffoldType()
        settings = self.getEditScaffoldSettings()
        oldValue = settings[key]
        # print('setScaffoldOption: key ', key, ' value ', str(value))
        newValue = None
        try:
            if type(oldValue) is bool:
                newValue = bool(value)
            elif type(oldValue) is int:
                newValue = int(value)
            elif type(oldValue) is float:
                newValue = float(value)
            elif type(oldValue) is str:
                newValue = str(value)
            else:
                newValue = value
        except:
            print('setScaffoldOption: Invalid value')
            return
        settings[key] = newValue
        dependentChanges = scaffoldType.checkOptions(settings)
        # print('final value = ', settings[key])
        if settings[key] != oldValue:
            self._clearMeshEdits()
            self._useCustomScaffoldPackage()
            self._generateMesh()
        return dependentChanges

    def getDeleteElementsRangesText(self):
        return self._settings['deleteElementRanges']

    def _parseDeleteElementsRangesText(self, elementRangesTextIn):
        """
        :return: True if ranges changed, otherwise False
        """
        elementRanges = []
        for elementRangeText in elementRangesTextIn.split(','):
            try:
                elementRangeEnds = elementRangeText.split('-')
                # remove trailing non-numeric characters, workaround for select 's' key ending up there
                for e in range(len(elementRangeEnds)):
                    size = len(elementRangeEnds[e])
                    for i in range(size):
                        if elementRangeEnds[e][size - i - 1] in string.digits:
                            break;
                    if i > 0:
                        elementRangeEnds[e] = elementRangeEnds[e][:(size - i)]
                elementRangeStart = int(elementRangeEnds[0])
                if len(elementRangeEnds) > 1:
                    elementRangeStop = int(elementRangeEnds[1])
                else:
                    elementRangeStop = elementRangeStart
                if elementRangeStop >= elementRangeStart:
                    elementRanges.append([elementRangeStart, elementRangeStop])
                else:
                    elementRanges.append([elementRangeStop, elementRangeStart])
            except:
                pass
        elementRanges.sort()
        # merge adjacent or overlapping ranges:
        i = 1
        while i < len(elementRanges):
            if elementRanges[i][0] <= (elementRanges[i - 1][1] + 1):
                if elementRanges[i][1] > elementRanges[i - 1][1]:
                    elementRanges[i - 1][1] = elementRanges[i][1]
                elementRanges.pop(i)
            else:
                i += 1
        elementRangesText = ''
        first = True
        for elementRange in elementRanges:
            if first:
                first = False
            else:
                elementRangesText += ','
            elementRangesText += str(elementRange[0])
            if elementRange[1] != elementRange[0]:
                elementRangesText += '-' + str(elementRange[1])
        changed = self._deleteElementRanges != elementRanges
        self._deleteElementRanges = elementRanges
        self._settings['deleteElementRanges'] = elementRangesText
        return changed

    def setDeleteElementsRangesText(self, elementRangesTextIn):
        if self._parseDeleteElementsRangesText(elementRangesTextIn):
            self._generateMesh()

    def deleteElementsSelection(self):
        '''
        Add the elements in the scene selection to the delete element ranges and delete.
        '''
        fm = self._region.getFieldmodule()
        scene = self._region.getScene()
        mesh = self._getMesh()
        selectionGroup = scene.getSelectionField().castGroup()
        meshGroup = selectionGroup.getFieldElementGroup(mesh).getMeshGroup()
        if meshGroup.isValid() and (meshGroup.getSize() > 0):
            # convert selection to element ranges text
            # following assumes iteration is in identifier order!
            elementIter = meshGroup.createElementiterator()
            element = elementIter.next()
            lastIdentifier = startIdentifier = element.getIdentifier()
            elementRangesText = str(startIdentifier)
            element = elementIter.next()
            while element.isValid():
                identifier = element.getIdentifier()
                if identifier > (lastIdentifier + 1):
                    if lastIdentifier > startIdentifier:
                        elementRangesText += "-" + str(lastIdentifier)
                    startIdentifier = identifier
                    elementRangesText += "," + str(startIdentifier)
                lastIdentifier = identifier
                element = elementIter.next()
            if lastIdentifier > startIdentifier:
                elementRangesText += "-" + str(lastIdentifier)
            # append to current delete element ranges
            self.setDeleteElementsRangesText(self._settings['deleteElementRanges'] + "," + elementRangesText)

    def getRotationText(self):
        return ', '.join(STRING_FLOAT_FORMAT.format(value) for value in self._scaffoldPackages[-1].getRotation())

    def setRotationText(self, rotationTextIn):
        rotation = parseVector3(rotationTextIn, delimiter=",", defaultValue=0.0)
        if self._scaffoldPackages[-1].setRotation(rotation):
            self._setGraphicsTransformation()

    def getScaleText(self):
        return ', '.join(STRING_FLOAT_FORMAT.format(value) for value in self._scaffoldPackages[-1].getScale())

    def setScaleText(self, scaleTextIn):
        scale = parseVector3(scaleTextIn, delimiter=",", defaultValue=1.0)
        if self._scaffoldPackages[-1].setScale(scale):
            self._setGraphicsTransformation()

    def getTranslationText(self):
        return ', '.join(STRING_FLOAT_FORMAT.format(value) for value in self._scaffoldPackages[-1].getTranslation())

    def setTranslationText(self, translationTextIn):
        translation = parseVector3(translationTextIn, delimiter=",", defaultValue=0.0)
        if self._scaffoldPackages[-1].setTranslation(translation):
            self._setGraphicsTransformation()

    def registerCustomParametersCallback(self, customParametersCallback):
        self._customParametersCallback = customParametersCallback

    def registerSceneChangeCallback(self, sceneChangeCallback):
        self._sceneChangeCallback = sceneChangeCallback

    def registerTransformationChangeCallback(self, transformationChangeCallback):
        self._transformationChangeCallback = transformationChangeCallback

    def _getVisibility(self, graphicsName):
        return self._settings[graphicsName]

    def _setVisibility(self, graphicsName, show):
        self._settings[graphicsName] = show
        graphics = self._region.getScene().findGraphicsByName(graphicsName)
        graphics.setVisibilityFlag(show)

    def isDisplayMarkerPoints(self):
        return self._getVisibility('displayMarkerPoints')

    def setDisplayMarkerPoints(self, show):
        self._setVisibility('displayMarkerPoints', show)

    def isDisplayAxes(self):
        return self._getVisibility('displayAxes')

    def setDisplayAxes(self, show):
        self._setVisibility('displayAxes', show)

    def isDisplayElementNumbers(self):
        return self._getVisibility('displayElementNumbers')

    def setDisplayElementNumbers(self, show):
        self._setVisibility('displayElementNumbers', show)

    def isDisplayLines(self):
        return self._getVisibility('displayLines')

    def setDisplayLines(self, show):
        self._setVisibility('displayLines', show)

    def isDisplayLinesExterior(self):
        return self._settings['displayLinesExterior']

    def setDisplayLinesExterior(self, isExterior):
        self._settings['displayLinesExterior'] = isExterior
        lines = self._region.getScene().findGraphicsByName('displayLines')
        lines.setExterior(self.isDisplayLinesExterior())

    def isDisplayModelRadius(self):
        return self._getVisibility('displayModelRadius')

    def setDisplayModelRadius(self, show):
        if show != self._settings['displayModelRadius']:
            self._settings['displayModelRadius'] = show
            self._createGraphics()

    def isDisplayNodeDerivatives(self):
        return self._getVisibility('displayNodeDerivatives')

    def _setAllGraphicsVisibility(self, graphicsName, show):
        '''
        Ensure visibility of all graphics with graphicsName is set to boolean show.
        '''
        scene = self._region.getScene()
        graphics = scene.findGraphicsByName(graphicsName)
        while graphics.isValid():
            graphics.setVisibilityFlag(show)
            while True:
                graphics = scene.getNextGraphics(graphics)
                if (not graphics.isValid()) or (graphics.getName() == graphicsName):
                    break

    def setDisplayNodeDerivatives(self, show):
        self._settings['displayNodeDerivatives'] = show
        for nodeDerivativeLabel in self._nodeDerivativeLabels:
            self._setAllGraphicsVisibility('displayNodeDerivatives' + nodeDerivativeLabel, show and self.isDisplayNodeDerivativeLabels(nodeDerivativeLabel))

    def isDisplayNodeDerivativeLabels(self, nodeDerivativeLabel):
        '''
        :param nodeDerivativeLabel: Label from self._nodeDerivativeLabels ('D1', 'D2' ...)
        '''
        return nodeDerivativeLabel in self._settings['displayNodeDerivativeLabels']

    def setDisplayNodeDerivativeLabels(self, nodeDerivativeLabel, show):
        '''
        :param nodeDerivativeLabel: Label from self._nodeDerivativeLabels ('D1', 'D2' ...)
        '''
        shown = nodeDerivativeLabel in self._settings['displayNodeDerivativeLabels']
        if show:
            if not shown:
                # keep in same order as self._nodeDerivativeLabels
                nodeDerivativeLabels = []
                for label in self._nodeDerivativeLabels:
                    if (label == nodeDerivativeLabel) or self.isDisplayNodeDerivativeLabels(label):
                        nodeDerivativeLabels.append(label)
                self._settings['displayNodeDerivativeLabels'] = nodeDerivativeLabels
        else:
            if shown:
                self._settings['displayNodeDerivativeLabels'].remove(nodeDerivativeLabel)
        self._setAllGraphicsVisibility('displayNodeDerivatives' + nodeDerivativeLabel, show and self.isDisplayNodeDerivatives())

    def isDisplayNodeNumbers(self):
        return self._getVisibility('displayNodeNumbers')

    def setDisplayNodeNumbers(self, show):
        self._setVisibility('displayNodeNumbers', show)

    def isDisplayNodePoints(self):
        return self._getVisibility('displayNodePoints')

    def setDisplayNodePoints(self, show):
        self._setVisibility('displayNodePoints', show)

    def isDisplaySurfaces(self):
        return self._getVisibility('displaySurfaces')

    def setDisplaySurfaces(self, show):
        self._setVisibility('displaySurfaces', show)

    def isDisplaySurfacesExterior(self):
        return self._settings['displaySurfacesExterior']

    def setDisplaySurfacesExterior(self, isExterior):
        self._settings['displaySurfacesExterior'] = isExterior
        surfaces = self._region.getScene().findGraphicsByName('displaySurfaces')
        surfaces.setExterior(self.isDisplaySurfacesExterior() if (self.getMeshDimension() == 3) else False)

    def isDisplaySurfacesTranslucent(self):
        return self._settings['displaySurfacesTranslucent']

    def setDisplaySurfacesTranslucent(self, isTranslucent):
        self._settings['displaySurfacesTranslucent'] = isTranslucent
        surfaces = self._region.getScene().findGraphicsByName('displaySurfaces')
        surfacesMaterial = self._materialmodule.findMaterialByName('trans_blue' if isTranslucent else 'solid_blue')
        surfaces.setMaterial(surfacesMaterial)
        lines = self._region.getScene().findGraphicsByName('displayLines')
        lineattr = lines.getGraphicslineattributes()
        isTranslucentLines = isTranslucent and (lineattr.getShapeType() == lineattr.SHAPE_TYPE_CIRCLE_EXTRUSION)
        linesMaterial = self._materialmodule.findMaterialByName('trans_blue' if isTranslucentLines else 'default')
        lines.setMaterial(linesMaterial)

    def isDisplaySurfacesWireframe(self):
        return self._settings['displaySurfacesWireframe']

    def setDisplaySurfacesWireframe(self, isWireframe):
        self._settings['displaySurfacesWireframe'] = isWireframe
        surfaces = self._region.getScene().findGraphicsByName('displaySurfaces')
        surfaces.setRenderPolygonMode(Graphics.RENDER_POLYGON_MODE_WIREFRAME if isWireframe else Graphics.RENDER_POLYGON_MODE_SHADED)

    def isDisplayElementAxes(self):
        return self._getVisibility('displayElementAxes')

    def setDisplayElementAxes(self, show):
        self._setVisibility('displayElementAxes', show)

    def needPerturbLines(self):
        """
        Return if solid surfaces are drawn with lines, requiring perturb lines to be activated.
        """
        if self._region is None:
            return False
        mesh2d = self._region.getFieldmodule().findMeshByDimension(2)
        if mesh2d.getSize() == 0:
            return False
        return self.isDisplayLines() and self.isDisplaySurfaces() and not self.isDisplaySurfacesTranslucent()

    def _getMesh(self):
        fm = self._region.getFieldmodule()
        for dimension in range(3,0,-1):
            mesh = fm.findMeshByDimension(dimension)
            if mesh.getSize() > 0:
                break
        if mesh.getSize() == 0:
            mesh = fm.findMeshByDimension(3)
        return mesh

    def getMeshDimension(self):
        return self._getMesh().getDimension()

    def getNodeLocation(self, node_id):
        fm = self._region.getFieldmodule()
        with ChangeManager(fm):
            coordinates = fm.findFieldByName('coordinates')
            nodes = fm.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_NODES)
            node = nodes.findNodeByIdentifier(node_id)
            fc = fm.createFieldcache()
            fc.setNode(node)
            _, position = coordinates.evaluateReal(fc, 3)
        return self._getSceneTransformationFromAdjustedPosition(position)

    def getSettings(self):
        return self._settings

    def setSettings(self, settings):
        '''
        Called on loading settings from file.
        '''
        scaffoldPackage = settings.get('scaffoldPackage')
        if not scaffoldPackage:
            # migrate obsolete options to scaffoldPackage:
            scaffoldType = self._getScaffoldTypeByName(settings['meshTypeName'])
            del settings['meshTypeName']
            scaffoldSettings = settings['meshTypeOptions']
            del settings['meshTypeOptions']
            scaffoldPackage = ScaffoldPackage(scaffoldType, { 'scaffoldSettings' : scaffoldSettings })
            settings['scaffoldPackage'] = scaffoldPackage
        self._settings.update(settings)
        self._parseDeleteElementsRangesText(self._settings['deleteElementRanges'])
        # migrate old scale text, now held in scaffoldPackage
        oldScaleText = self._settings.get('scale')
        if oldScaleText:
            scaffoldPackage.setScale(parseVector3(oldScaleText, delimiter="*", defaultValue=1.0))
            del self._settings['scale']  # remove so can't overwrite scale next time
        self._scaffoldPackages = [ scaffoldPackage ]
        self._scaffoldPackageOptionNames = [ None ]
        self._checkCustomParameterSet()
        self._generateMesh()

    def _deleteElementsInRanges(self):
        '''
        If this is the root scaffold and there are ranges of element identifiers to delete,
        remove these from the model.
        Also remove marker group nodes embedded in those elements and any nodes used only by
        the deleted elements.
        '''
        if (len(self._deleteElementRanges) == 0) or (len(self._scaffoldPackages) > 1):
            return
        fm = self._region.getFieldmodule()
        mesh = self._getMesh()
        meshDimension = mesh.getDimension()
        nodes = fm.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_NODES)
        with ChangeManager(fm):
            # put the elements in a group and use subelement handling to get nodes in use by it
            destroyGroup = fm.createFieldGroup()
            destroyGroup.setSubelementHandlingMode(FieldGroup.SUBELEMENT_HANDLING_MODE_FULL)
            destroyElementGroup = destroyGroup.createFieldElementGroup(mesh)
            destroyMesh = destroyElementGroup.getMeshGroup()
            elementIter = mesh.createElementiterator()
            element = elementIter.next()
            while element.isValid():
                identifier = element.getIdentifier()
                for deleteElementRange in self._deleteElementRanges:
                    if (identifier >= deleteElementRange[0]) and (identifier <= deleteElementRange[1]):
                        destroyMesh.addElement(element)
                element = elementIter.next()
            del elementIter
            #print("Deleting", destroyMesh.getSize(), "element(s)")
            if destroyMesh.getSize() > 0:
                destroyNodeGroup = destroyGroup.getFieldNodeGroup(nodes)
                destroyNodes = destroyNodeGroup.getNodesetGroup()
                markerGroup = fm.findFieldByName("marker").castGroup()
                if markerGroup.isValid():
                    markerNodes = markerGroup.getFieldNodeGroup(nodes).getNodesetGroup()
                    markerLocation = fm.findFieldByName("marker_location")
                    #markerName = fm.findFieldByName("marker_name")
                    if markerNodes.isValid() and markerLocation.isValid():
                        fieldcache = fm.createFieldcache()
                        nodeIter = markerNodes.createNodeiterator()
                        node = nodeIter.next()
                        while node.isValid():
                            fieldcache.setNode(node)
                            element, xi = markerLocation.evaluateMeshLocation(fieldcache, meshDimension)
                            if element.isValid() and destroyMesh.containsElement(element):
                                #print("Destroy marker '" + markerName.evaluateString(fieldcache) + "' node", node.getIdentifier(), "in destroyed element", element.getIdentifier(), "at", xi)
                                destroyNodes.addNode(node)  # so destroyed with others below; can't do here as
                            node = nodeIter.next()
                        del nodeIter
                        del fieldcache
                # must destroy elements first as Zinc won't destroy nodes that are in use
                mesh.destroyElementsConditional(destroyElementGroup)
                nodes.destroyNodesConditional(destroyNodeGroup)
                # clean up group so no external code hears is notified of its existence
                del destroyNodes
                del destroyNodeGroup
            del destroyMesh
            del destroyElementGroup
            del destroyGroup

    def _generateMesh(self):
        scaffoldPackage = self._scaffoldPackages[-1]
        if self._region:
            self._parent_region.removeChild(self._region)
        self._region = self._parent_region.createChild(self._region_name)
        self._scene = self._region.getScene()
        fm = self._region.getFieldmodule()
        with ChangeManager(fm):
            # logger = self._context.getLogger()
            annotationGroups = scaffoldPackage.generate(self._region, applyTransformation=False)
            # loggerMessageCount = logger.getNumberOfMessages()
            # if loggerMessageCount > 0:
            #     for i in range(1, loggerMessageCount + 1):
            #         print(logger.getMessageTypeAtIndex(i), logger.getMessageTextAtIndex(i))
            #     logger.removeAllMessages()
            self._deleteElementsInRanges()
            fm.defineAllFaces()
            if annotationGroups is not None:
                for annotationGroup in annotationGroups:
                    annotationGroup.addSubelements()
            self._annotationGroups = annotationGroups
        self._createGraphics()
        if self._sceneChangeCallback:
            self._sceneChangeCallback()

    def _setGraphicsTransformation(self):
        '''
        Establish 4x4 graphics transformation for current scaffold package.
        '''
        transformationMatrix = None
        for scaffoldPackage in reversed(self._scaffoldPackages):
            mat = scaffoldPackage.getTransformationMatrix()
            if mat:
                transformationMatrix = matrix_mult(mat, transformationMatrix) if transformationMatrix else mat
        scene = self._region.getScene()
        if transformationMatrix:
            # flatten to list of 16 components for passing to Zinc
            scene.setTransformationMatrix(transformationMatrix[0] + transformationMatrix[1] + transformationMatrix[2] + transformationMatrix[3])
        else:
            scene.clearTransformation()

    def _createGraphics(self):
        fm = self._region.getFieldmodule()
        with ChangeManager(fm):
            meshDimension = self.getMeshDimension()
            coordinates = fm.findFieldByName('coordinates').castFiniteElement()
            componentsCount = coordinates.getNumberOfComponents()
            nodes = fm.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_NODES)
            fieldcache = fm.createFieldcache()

            # determine field derivatives for all versions in use: fairly expensive
            # fields in same order as self._nodeDerivativeLabels
            nodeDerivatives = [ Node.VALUE_LABEL_D_DS1, Node.VALUE_LABEL_D_DS2, Node.VALUE_LABEL_D_DS3,
                Node.VALUE_LABEL_D2_DS1DS2, Node.VALUE_LABEL_D2_DS1DS3, Node.VALUE_LABEL_D2_DS2DS3, Node.VALUE_LABEL_D3_DS1DS2DS3 ]
            nodeDerivativeFields = [ [ fm.createFieldNodeValue(coordinates, nodeDerivative, 1) ] for nodeDerivative in nodeDerivatives ]
            derivativesCount = len(nodeDerivatives)
            maxVersions = [ 1 for nodeDerivative in nodeDerivatives ]
            lastVersion = 1
            version = 2
            while True:
                nodeIter = nodes.createNodeiterator()
                node = nodeIter.next()
                foundCount = sum((1 if (v < lastVersion) else 0) for v in maxVersions)
                while (node.isValid()) and (foundCount < derivativesCount):
                    fieldcache.setNode(node)
                    for d in range(derivativesCount):
                        if maxVersions[d] == lastVersion:  # only look one higher than last version found
                            result, values = coordinates.getNodeParameters(fieldcache, -1, nodeDerivatives[d], version, componentsCount)
                            if (result == RESULT_OK) or (result == RESULT_WARNING_PART_DONE):
                                maxVersions[d] = version
                                nodeDerivativeFields[d].append(fm.createFieldNodeValue(coordinates, nodeDerivatives[d], version))
                                foundCount += 1
                    node = nodeIter.next()
                if foundCount >= derivativesCount:
                    break
                lastVersion = version
                version += 1
            elementDerivativeFields = []
            for d in range(meshDimension):
                elementDerivativeFields.append(fm.createFieldDerivative(coordinates, d + 1))
            elementDerivativesField = fm.createFieldConcatenate(elementDerivativeFields)
            cmiss_number = fm.findFieldByName('cmiss_number')
            markerGroup = fm.findFieldByName('marker').castGroup()
            markerName = findOrCreateFieldStoredString(fm, 'marker_name')
            radius = fm.findFieldByName('radius')
            markerLocation = findOrCreateFieldStoredMeshLocation(fm, self._getMesh(), name='marker_location')
            markerHostCoordinates = fm.createFieldEmbedded(coordinates, markerLocation)

            # get sizing for axes
            axesScale = 1.0
            minX, maxX = evaluateFieldNodesetRange(coordinates, nodes)
            if componentsCount == 1:
                maxRange = maxX - minX
            else:
                maxRange = maxX[0] - minX[0]
                for c in range(1, componentsCount):
                    maxRange = max(maxRange, maxX[c] - minX[c])
            if maxRange > 0.0:
                while axesScale*10.0 < maxRange:
                    axesScale *= 10.0
                while axesScale*0.1 > maxRange:
                    axesScale *= 0.1

            # fixed width glyph size is based on average element size in all dimensions
            mesh1d = fm.findMeshByDimension(1)
            meanLineLength = 0.0
            lineCount = mesh1d.getSize()
            if lineCount > 0:
                one = fm.createFieldConstant(1.0)
                sumLineLength = fm.createFieldMeshIntegral(one, coordinates, mesh1d)
                result, totalLineLength = sumLineLength.evaluateReal(fieldcache, 1)
                glyphWidth = 0.1*totalLineLength/lineCount
                del sumLineLength
                del one
            if (lineCount == 0) or (glyphWidth == 0.0):
                # use function of coordinate range if no elements
                if componentsCount == 1:
                    maxScale = maxX - minX
                else:
                    first = True
                    for c in range(componentsCount):
                        scale = maxX[c] - minX[c]
                        if first or (scale > maxScale):
                            maxScale = scale
                            first = False
                if maxScale == 0.0:
                    maxScale = 1.0
                glyphWidth = 0.01*maxScale
            del fieldcache

        # make graphics
        scene = self._region.getScene()
        with ChangeManager(scene):
            scene.removeAllGraphics()

            self._setGraphicsTransformation()
            axes = scene.createGraphicsPoints()
            axes.setScenecoordinatesystem(SCENECOORDINATESYSTEM_WORLD)
            pointattr = axes.getGraphicspointattributes()
            pointattr.setGlyphShapeType(Glyph.SHAPE_TYPE_AXES_XYZ)
            pointattr.setBaseSize([ axesScale ])
            pointattr.setLabelText(1, '  ' + str(axesScale))
            axes.setMaterial(self._materialmodule.findMaterialByName('grey50'))
            axes.setName('displayAxes')
            axes.setVisibilityFlag(self.isDisplayAxes())

            lines = scene.createGraphicsLines()
            lines.setCoordinateField(coordinates)
            lines.setExterior(self.isDisplayLinesExterior())
            lineattr = lines.getGraphicslineattributes()
            if self.isDisplayModelRadius() and radius.isValid():
                lineattr.setShapeType(lineattr.SHAPE_TYPE_CIRCLE_EXTRUSION)
                lineattr.setBaseSize([ 0.0 ])
                lineattr.setScaleFactors([ 2.0 ])
                lineattr.setOrientationScaleField(radius)
            isTranslucentLines = self.isDisplaySurfacesTranslucent() and (lineattr.getShapeType() == lineattr.SHAPE_TYPE_CIRCLE_EXTRUSION)
            linesMaterial = self._materialmodule.findMaterialByName('trans_blue' if isTranslucentLines else 'default')
            lines.setMaterial(linesMaterial)
            lines.setName('displayLines')
            lines.setVisibilityFlag(self.isDisplayLines())

            nodePoints = scene.createGraphicsPoints()
            nodePoints.setFieldDomainType(Field.DOMAIN_TYPE_NODES)
            nodePoints.setCoordinateField(coordinates)
            pointattr = nodePoints.getGraphicspointattributes()
            pointattr.setGlyphShapeType(Glyph.SHAPE_TYPE_SPHERE)
            if self.isDisplayModelRadius() and radius.isValid():
                pointattr.setBaseSize([ 0.0 ])
                pointattr.setScaleFactors([ 2.0 ])
                pointattr.setOrientationScaleField(radius)
            else:
                pointattr.setBaseSize([ glyphWidth ])
            nodePoints.setMaterial(self._materialmodule.findMaterialByName('white'))
            nodePoints.setName('displayNodePoints')
            nodePoints.setVisibilityFlag(self.isDisplayNodePoints())

            nodeNumbers = scene.createGraphicsPoints()
            nodeNumbers.setFieldDomainType(Field.DOMAIN_TYPE_NODES)
            nodeNumbers.setCoordinateField(coordinates)
            pointattr = nodeNumbers.getGraphicspointattributes()
            pointattr.setLabelField(cmiss_number)
            pointattr.setGlyphShapeType(Glyph.SHAPE_TYPE_NONE)
            nodeNumbers.setMaterial(self._materialmodule.findMaterialByName('green'))
            nodeNumbers.setName('displayNodeNumbers')
            nodeNumbers.setVisibilityFlag(self.isDisplayNodeNumbers())

            # names in same order as self._nodeDerivativeLabels 'D1', 'D2', 'D3', 'D12', 'D13', 'D23', 'D123' and nodeDerivativeFields
            nodeDerivativeMaterialNames = [ 'gold', 'silver', 'green', 'cyan', 'magenta', 'yellow', 'blue' ]
            derivativeScales = [ 1.0, 1.0, 1.0, 0.5, 0.5, 0.5, 0.25 ]
            for i in range(len(self._nodeDerivativeLabels)):
                nodeDerivativeLabel = self._nodeDerivativeLabels[i]
                maxVersions = len(nodeDerivativeFields[i])
                for v in range(maxVersions):
                    nodeDerivatives = scene.createGraphicsPoints()
                    nodeDerivatives.setFieldDomainType(Field.DOMAIN_TYPE_NODES)
                    nodeDerivatives.setCoordinateField(coordinates)
                    pointattr = nodeDerivatives.getGraphicspointattributes()
                    pointattr.setGlyphShapeType(Glyph.SHAPE_TYPE_ARROW_SOLID)
                    pointattr.setOrientationScaleField(nodeDerivativeFields[i][v])
                    pointattr.setBaseSize([0.0, glyphWidth, glyphWidth])
                    pointattr.setScaleFactors([ derivativeScales[i], 0.0, 0.0 ])
                    if maxVersions > 1:
                        pointattr.setLabelOffset([ 1.05, 0.0, 0.0 ])
                        pointattr.setLabelText(1, str(v + 1))
                    material = self._materialmodule.findMaterialByName(nodeDerivativeMaterialNames[i])
                    nodeDerivatives.setMaterial(material)
                    nodeDerivatives.setSelectedMaterial(material)
                    nodeDerivatives.setName('displayNodeDerivatives' + nodeDerivativeLabel)
                    nodeDerivatives.setVisibilityFlag(self.isDisplayNodeDerivatives() and self.isDisplayNodeDerivativeLabels(nodeDerivativeLabel))

            elementNumbers = scene.createGraphicsPoints()
            elementNumbers.setFieldDomainType(Field.DOMAIN_TYPE_MESH_HIGHEST_DIMENSION)
            elementNumbers.setCoordinateField(coordinates)
            pointattr = elementNumbers.getGraphicspointattributes()
            pointattr.setLabelField(cmiss_number)
            pointattr.setGlyphShapeType(Glyph.SHAPE_TYPE_NONE)
            elementNumbers.setMaterial(self._materialmodule.findMaterialByName('cyan'))
            elementNumbers.setName('displayElementNumbers')
            elementNumbers.setVisibilityFlag(self.isDisplayElementNumbers())
            surfaces = scene.createGraphicsSurfaces()
            surfaces.setCoordinateField(coordinates)
            surfaces.setRenderPolygonMode(Graphics.RENDER_POLYGON_MODE_WIREFRAME if self.isDisplaySurfacesWireframe() else Graphics.RENDER_POLYGON_MODE_SHADED)
            surfaces.setExterior(self.isDisplaySurfacesExterior() if (meshDimension == 3) else False)
            surfacesMaterial = self._materialmodule.findMaterialByName('trans_blue' if self.isDisplaySurfacesTranslucent() else 'solid_blue')
            surfaces.setMaterial(surfacesMaterial)
            surfaces.setName('displaySurfaces')
            surfaces.setVisibilityFlag(self.isDisplaySurfaces())

            elementAxes = scene.createGraphicsPoints()
            elementAxes.setFieldDomainType(Field.DOMAIN_TYPE_MESH_HIGHEST_DIMENSION)
            elementAxes.setCoordinateField(coordinates)
            pointattr = elementAxes.getGraphicspointattributes()
            pointattr.setGlyphShapeType(Glyph.SHAPE_TYPE_AXES_123)
            pointattr.setOrientationScaleField(elementDerivativesField)
            if meshDimension == 1:
                pointattr.setBaseSize([0.0, 2*glyphWidth, 2*glyphWidth])
                pointattr.setScaleFactors([0.25, 0.0, 0.0])
            elif meshDimension == 2:
                pointattr.setBaseSize([0.0, 0.0, 2*glyphWidth])
                pointattr.setScaleFactors([0.25, 0.25, 0.0])
            else:
                pointattr.setBaseSize([0.0, 0.0, 0.0])
                pointattr.setScaleFactors([0.25, 0.25, 0.25])
            elementAxes.setMaterial(self._materialmodule.findMaterialByName('yellow'))
            elementAxes.setName('displayElementAxes')
            elementAxes.setVisibilityFlag(self.isDisplayElementAxes())

            # marker points
            markerPoints = scene.createGraphicsPoints()
            markerPoints.setFieldDomainType(Field.DOMAIN_TYPE_NODES)
            markerPoints.setSubgroupField(markerGroup)
            markerPoints.setCoordinateField(markerHostCoordinates)
            pointattr = markerPoints.getGraphicspointattributes()
            pointattr.setLabelText(1, '  ')
            pointattr.setLabelField(markerName)
            pointattr.setGlyphShapeType(Glyph.SHAPE_TYPE_CROSS)
            pointattr.setBaseSize(2*glyphWidth)
            markerPoints.setMaterial(self._materialmodule.findMaterialByName('yellow'))
            markerPoints.setName('displayMarkerPoints')
            markerPoints.setVisibilityFlag(self.isDisplayMarkerPoints())


    def updateSettingsBeforeWrite(self):
        self._updateMeshEdits()

    def done(self):
        '''
        Finish generating mesh by applying transformation.
        '''
        assert 1 == len(self._scaffoldPackages)
        self._scaffoldPackages[0].applyTransformation(self._region)

    def writeModel(self, file_name):
        self._region.writeFile(file_name)

    def exportToVtk(self, filenameStem):
        base_name = os.path.basename(filenameStem)
        description = 'Scaffold ' + self._scaffoldPackages[0].getScaffoldType().getName() + ': ' + base_name
        exportvtk = ExportVtk(self._region, description, self._annotationGroups)
        exportvtk.writeFile(filenameStem + '.vtk')

def exnodeStringFromGroup(region, groupName, fieldNames):
    '''
    Serialise field within group of groupName to a string.
    :param fieldNames: List of fieldNames to output.
    :param groupName: Name of group to output.
    :return: The string.
    '''
    sir = region.createStreaminformationRegion()
    srm = sir.createStreamresourceMemory()
    sir.setResourceGroupName(srm, groupName)
    sir.setResourceFieldNames(srm, fieldNames)
    region.write(sir)
    result, exString = srm.getBuffer()
    return exString
