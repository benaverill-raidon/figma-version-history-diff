// Variable Exporter - main thread.
//
// Runs inside the Figma editor, where the Plugin API can read local variables
// on any plan tier (unlike the REST /variables/local endpoint, which is
// Enterprise-only). Collects everything figma_diff.py needs and hands it to
// the UI thread, which is the only side that can trigger a file download.

var SCHEMA = 'figma-variable-export/1';

figma.showUI(__html__, { width: 340, height: 420, themeColors: true });

function sanitize(name) {
  return String(name || 'figma-file')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'figma-file';
}

function serializeCollection(collection) {
  return {
    id: collection.id,
    name: collection.name,
    key: collection.key,
    defaultModeId: collection.defaultModeId,
    modes: collection.modes.map(function (mode) {
      return { modeId: mode.modeId, name: mode.name };
    }),
    variableIds: collection.variableIds,
    remote: collection.remote,
    hiddenFromPublishing: collection.hiddenFromPublishing
  };
}

function serializeVariable(variable) {
  return {
    id: variable.id,
    name: variable.name,
    key: variable.key,
    description: variable.description,
    resolvedType: variable.resolvedType,
    variableCollectionId: variable.variableCollectionId,
    // Aliases stay as { type: 'VARIABLE_ALIAS', id } and colors as
    // { r, g, b, a } - both are plain JSON, and figma_diff.py resolves them.
    valuesByMode: variable.valuesByMode,
    scopes: variable.scopes,
    codeSyntax: variable.codeSyntax,
    remote: variable.remote,
    hiddenFromPublishing: variable.hiddenFromPublishing
  };
}

async function collect() {
  var collections = await figma.variables.getLocalVariableCollectionsAsync();
  var variables = await figma.variables.getLocalVariablesAsync();

  return {
    schema: SCHEMA,
    exportedAt: new Date().toISOString(),
    fileName: figma.root.name,
    fileKey: figma.fileKey || null,
    suggestedFilename: sanitize(figma.root.name) + '_variables_export.json',
    collections: collections.map(serializeCollection),
    variables: variables.map(serializeVariable)
  };
}

async function send() {
  try {
    var payload = await collect();
    figma.ui.postMessage({ type: 'export-data', payload: payload });
  } catch (error) {
    figma.ui.postMessage({
      type: 'export-error',
      message: (error && error.message) || String(error)
    });
  }
}

figma.ui.onmessage = function (message) {
  if (!message || !message.type) return;

  if (message.type === 'refresh') {
    send();
  } else if (message.type === 'downloaded') {
    figma.notify('Exported ' + message.count + ' variables. Rename it to ' +
      '<file>_variables.json and move it into variable_exports/.');
  } else if (message.type === 'close') {
    figma.closePlugin();
  }
};

send();
