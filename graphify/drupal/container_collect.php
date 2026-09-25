<?php
// graphify-drupal: the container collector (P3 spec S5).
//
// Run by `graphify drupal container` through `drush php:eval`, which hands it
// over as one argument with this opening tag stripped and a header line
// `$GRAPHIFY_HOOKS = [...];` prepended (the hook names of the static registry,
// used only by the pre-11.1 hook fallback). `graphify drupal container
// --print-script` prints it for running by hand.
//
// It walks what a booted Drupal has already built -- the dumped container
// definition, the router table, the extension lists, the stored hook list,
// the plugin managers' definitions and the event dispatcher's listeners --
// and prints one JSON object on stdout and nothing else. Each source runs in
// its own try: a throwing source is recorded in `errors` and leaves its key
// empty. It invokes no hook and writes nothing of its own. Scalar service
// arguments, configuration, labels and content are never read out.
//
// Paths are absolute here (in-container); Python makes them relative to
// `site.composer_root` and drops `site`.

if (!isset($GRAPHIFY_HOOKS) || !is_array($GRAPHIFY_HOOKS)) {
  $GRAPHIFY_HOOKS = [];
}

ob_start();

$__g = [
  'schema_version' => 1,
  'site' => [],
  'services' => [],
  'aliases' => [],
  'routes' => [],
  'extensions' => [],
  'hooks' => [],
  'plugins' => [],
  'subscribers' => [],
  'closures' => [],
  'errors' => [],
];

$__g_error = function (string $source, \Throwable $e) use (&$__g) {
  $__g['errors'][] = [
    'source' => $source,
    'class' => get_class($e),
    'message' => $e->getMessage(),
  ];
};

$__g_str = function ($v) {
  return (is_string($v) && $v !== '') ? $v : NULL;
};

// The file that declares `$class`, or NULL when it cannot be loaded.
$__g_class_file = function ($class) {
  if (!is_string($class) || $class === '') {
    return NULL;
  }
  try {
    if (!class_exists($class, TRUE) && !interface_exists($class, FALSE) && !trait_exists($class, FALSE)) {
      return NULL;
    }
    $file = (new \ReflectionClass($class))->getFileName();
    return $file === FALSE ? NULL : $file;
  }
  catch (\Throwable $e) {
    return NULL;
  }
};

// The file that declares `Class::method` or a function, or NULL.
$__g_callable_file = function ($identifier) use ($__g_class_file) {
  if (!is_string($identifier) || $identifier === '') {
    return NULL;
  }
  try {
    if (str_contains($identifier, '::')) {
      [$class, $method] = explode('::', $identifier, 2);
      if ($__g_class_file($class) === NULL || !method_exists($class, $method)) {
        return $__g_class_file($class);
      }
      $file = (new \ReflectionMethod($class, $method))->getFileName();
      return $file === FALSE ? NULL : $file;
    }
    if (!function_exists($identifier)) {
      return NULL;
    }
    $file = (new \ReflectionFunction($identifier))->getFileName();
    return $file === FALSE ? NULL : $file;
  }
  catch (\Throwable $e) {
    return NULL;
  }
};

// `[identifier, file]` for a hook listener handed over by invokeAllWith().
$__g_listener = function ($listener) use ($__g_callable_file) {
  if (is_array($listener) && isset($listener[0], $listener[1]) && is_string($listener[1])) {
    $class = is_object($listener[0]) ? get_class($listener[0]) : (is_string($listener[0]) ? $listener[0] : NULL);
    if ($class !== NULL) {
      $identifier = $class . '::' . $listener[1];
      return [$identifier, $__g_callable_file($identifier)];
    }
  }
  if (is_string($listener)) {
    return [$listener, $__g_callable_file($listener)];
  }
  if ($listener instanceof \Closure) {
    try {
      $rf = new \ReflectionFunction($listener);
      $name = $rf->getName();
      $scope = $rf->getClosureScopeClass();
      $identifier = str_contains($name, '{closure')
        ? '{closure}'
        : ($scope !== NULL ? $scope->getName() . '::' . $name : $name);
      $file = $rf->getFileName();
      return [$identifier, $file === FALSE ? NULL : $file];
    }
    catch (\Throwable $e) {
      return ['{closure}', NULL];
    }
  }
  if (is_object($listener)) {
    $identifier = get_class($listener) . '::__invoke';
    return [$identifier, $__g_callable_file($identifier)];
  }
  return [NULL, NULL];
};

// Installed extensions by name, for `provider`.
$__g_known = [];
try {
  foreach (array_keys(\Drupal::moduleHandler()->getModuleList()) as $name) {
    $__g_known[$name] = TRUE;
  }
}
catch (\Throwable $e) {
  // Left empty: every provider is then NULL. The extensions source reports
  // a broken bootstrap on its own.
}
try {
  foreach (array_keys(\Drupal::service('theme_handler')->listInfo()) as $name) {
    $__g_known[$name] = TRUE;
  }
}
catch (\Throwable $e) {
}

// The first `Drupal\<ext>\` namespace segment of a class or callable, when
// `<ext>` is an installed extension.
$__g_provider = function ($class) use (&$__g_known) {
  if (!is_string($class)) {
    return NULL;
  }
  if (preg_match('/^\\\\?Drupal\\\\([A-Za-z0-9_]+)\\\\/', $class, $m) && isset($__g_known[$m[1]])) {
    return $m[1];
  }
  return NULL;
};

// ---- services and aliases -------------------------------------------------

// References inside a dumped argument list. OptimizedPhpArrayDumper writes a
// reference as an object `{type: service|private_service|service_closure,
// id}` and a parameter as `{type: parameter, name}`; collections and
// iterators nest under `value`. Scalars are never kept.
$__g_walk = function ($value, array &$refs) use (&$__g_walk) {
  if (is_string($value)) {
    if (preg_match('/^@\??([A-Za-z0-9_.\\\\]+)$/', $value, $m)) {
      $refs[] = $m[1];
    }
    elseif (preg_match('/^%[^%]+%$/', $value)) {
      $refs[] = $value;
    }
    return;
  }
  if ($value instanceof \__PHP_Incomplete_Class) {
    return;
  }
  if (is_object($value)) {
    $value = get_object_vars($value);
  }
  if (!is_array($value)) {
    return;
  }
  $type = $value['type'] ?? NULL;
  if (is_string($type)) {
    switch ($type) {
      case 'service':
      case 'service_closure':
        if (is_string($value['id'] ?? NULL)) {
          $refs[] = $value['id'];
        }
        return;

      case 'private_service':
        // A private service keeps its id; an anonymous inline definition is
        // `private__<hash>` and names nothing.
        if (is_string($value['id'] ?? NULL) && !str_starts_with($value['id'], 'private__')) {
          $refs[] = $value['id'];
        }
        return;

      case 'parameter':
        if (is_string($value['name'] ?? NULL)) {
          $refs[] = '%' . $value['name'] . '%';
        }
        return;

      case 'collection':
      case 'iterator':
        $__g_walk($value['value'] ?? [], $refs);
        return;

      case 'raw':
        return;
    }
  }
  foreach ($value as $item) {
    $__g_walk($item, $refs);
  }
};

$__g_scalars = function ($attributes) {
  $out = [];
  if (is_array($attributes)) {
    foreach ($attributes as $k => $v) {
      if (is_string($k) && (is_scalar($v) || $v === NULL)) {
        $out[$k] = $v;
      }
    }
  }
  return (object) $out;
};

$__g_def = NULL;
try {
  $__g_def = \Drupal::service('kernel')->getCachedContainerDefinition();
  if (!is_array($__g_def)) {
    $__g_def = NULL;
    throw new \RuntimeException('the kernel has no cached container definition');
  }

  $services = [];
  foreach (($__g_def['services'] ?? []) as $id => $s) {
    if (!is_string($id)) {
      continue;
    }
    if (is_string($s)) {
      $s = @unserialize($s, ['allowed_classes' => ['stdClass']]);
    }
    if (!is_array($s)) {
      continue;
    }
    $class = $__g_str($s['class'] ?? NULL);
    $refs = [];
    $__g_walk($s['arguments'] ?? [], $refs);

    // The runtime dump carries no tags and no decoration (the dumper drops
    // tags, and DecoratorServicePass has already resolved decoration); both
    // are read in case a definition does carry them.
    $tags = [];
    foreach ((is_array($s['tags'] ?? NULL) ? $s['tags'] : []) as $name => $list) {
      if (is_string($name) && is_array($list)) {
        foreach ($list as $attributes) {
          $tags[] = ['name' => $name, 'attributes' => $__g_scalars($attributes)];
        }
      }
      elseif (is_array($list) && is_string($list['name'] ?? NULL)) {
        $attributes = $list;
        unset($attributes['name']);
        $tags[] = ['name' => $list['name'], 'attributes' => $__g_scalars($attributes)];
      }
    }
    $decorates = $s['decorates'] ?? ($s['decorated_service'] ?? NULL);
    if (is_array($decorates)) {
      $decorates = $decorates[0] ?? NULL;
    }

    $services[$id] = [
      'id' => $id,
      'class' => $class,
      'file' => $__g_class_file($class),
      'arguments' => array_values(array_unique($refs)),
      'tags' => $tags,
      'decorates' => $__g_str($decorates),
      'provider' => $__g_provider($class),
    ];
  }

  // Decoration survives in the dump as its result: the decorated id is an
  // alias of the decorator, which injects `<decorator>.inner`.
  $targets = [];
  foreach (($__g_def['aliases'] ?? []) as $alias => $target) {
    if (is_string($alias) && is_string($target)) {
      $targets[$target][] = $alias;
    }
  }
  foreach ($services as $id => &$service) {
    if ($service['decorates'] === NULL && in_array($id . '.inner', $service['arguments'], TRUE)) {
      $candidates = $targets[$id] ?? [];
      if (count($candidates) > 1) {
        $candidates = array_values(array_filter($candidates, fn ($a) => !str_contains($a, '\\')));
      }
      if (count($candidates) === 1) {
        $service['decorates'] = $candidates[0];
      }
    }
  }
  unset($service);

  $__g['services'] = array_values($services);
}
catch (\Throwable $e) {
  $__g_error('services', $e);
}

try {
  if ($__g_def === NULL) {
    throw new \RuntimeException('the kernel has no cached container definition');
  }
  foreach (($__g_def['aliases'] ?? []) as $alias => $target) {
    if (is_string($alias) && is_string($target)) {
      $__g['aliases'][$alias] = $target;
    }
  }
}
catch (\Throwable $e) {
  $__g_error('aliases', $e);
}

// ---- routes ---------------------------------------------------------------

$__g_route_defaults = ['_controller', '_form', '_entity_form', '_entity_list', '_entity_view', '_title_callback'];
$__g_route_requirements = ['_permission', '_custom_access', '_entity_access'];
try {
  foreach (\Drupal::service('router.route_provider')->getAllRoutes() as $name => $route) {
    $defaults = [];
    foreach ($route->getDefaults() as $k => $v) {
      if (in_array($k, $__g_route_defaults, TRUE) && is_string($v)) {
        $defaults[$k] = $v;
      }
    }
    $requirements = [];
    foreach ($route->getRequirements() as $k => $v) {
      if (is_string($k) && is_string($v)
          && (in_array($k, $__g_route_requirements, TRUE) || str_starts_with($k, '_access'))) {
        $requirements[$k] = $v;
      }
    }
    $provider = NULL;
    foreach (['_controller', '_form'] as $k) {
      if ($provider === NULL && isset($defaults[$k])) {
        $provider = $__g_provider(explode('::', $defaults[$k], 2)[0]);
      }
    }
    $__g['routes'][] = [
      'name' => (string) $name,
      'path' => $route->getPath(),
      'defaults' => (object) $defaults,
      'requirements' => (object) $requirements,
      'provider' => $provider,
    ];
  }
}
catch (\Throwable $e) {
  $__g_error('routes', $e);
}

// ---- extensions -----------------------------------------------------------

$__g_enabled = [];
$__g_seen = [];
foreach (['module', 'theme', 'profile'] as $__g_type) {
  try {
    foreach (\Drupal::service('extension.list.' . $__g_type)->getList() as $name => $ext) {
      // The active profile is in the module list too, typed `profile`.
      $type = method_exists($ext, 'getType') ? $ext->getType() : $__g_type;
      $key = $type . ':' . $name;
      if (isset($__g_seen[$key])) {
        continue;
      }
      $__g_seen[$key] = TRUE;
      $dependencies = [];
      foreach ((is_array($ext->info['dependencies'] ?? NULL) ? $ext->info['dependencies'] : []) as $d) {
        if (!is_string($d)) {
          continue;
        }
        // `project:name (>=1.0)` names `name`.
        $d = trim(preg_replace('/\s*\(.*\)\s*$/', '', $d));
        $colon = strpos($d, ':');
        $dependencies[] = $colon === FALSE ? $d : substr($d, $colon + 1);
      }
      $status = (int) ($ext->status ?? 0);
      if ($status === 1) {
        $__g_enabled[] = $key;
      }
      $__g['extensions'][] = [
        'name' => (string) $name,
        'type' => $type,
        'path' => DRUPAL_ROOT . '/' . dirname($ext->getPathname()),
        'status' => $status,
        'weight' => (int) ($ext->weight ?? 0),
        'dependencies' => $dependencies,
      ];
    }
  }
  catch (\Throwable $e) {
    $__g_error('extensions', $e);
  }
}

// ---- hooks ----------------------------------------------------------------

try {
  // 11.1+: HookCollectorPass stores hook => [identifier => module], already
  // in execution order; the identifier is `Class::method` or a function.
  $list = \Drupal::keyValue('hook_data')->get('hook_list');
  if (is_array($list)) {
    foreach ($list as $hook => $implementations) {
      if (!is_string($hook) || !is_array($implementations)) {
        continue;
      }
      foreach ($implementations as $identifier => $module) {
        if (!is_string($identifier)) {
          continue;
        }
        $__g['hooks'][$hook][] = [
          'module' => is_string($module) ? $module : NULL,
          'callable' => $identifier,
          'file' => $__g_callable_file($identifier),
        ];
      }
    }
  }
  else {
    // Before 11.1: ask the module handler, one hook at a time. The callback
    // records the listener and never calls it.
    $handler = \Drupal::moduleHandler();
    foreach ($GRAPHIFY_HOOKS as $hook) {
      if (!is_string($hook) || $hook === '') {
        continue;
      }
      $handler->invokeAllWith($hook, function (callable $listener, string $module) use (&$__g, $hook, $__g_listener) {
        [$identifier, $file] = $__g_listener($listener);
        $__g['hooks'][$hook][] = ['module' => $module, 'callable' => $identifier, 'file' => $file];
      });
    }
  }
}
catch (\Throwable $e) {
  $__g_error('hooks', $e);
}

// ---- plugins --------------------------------------------------------------

$__g_get = function ($definition, string $key, string $method = '') {
  try {
    if (is_array($definition)) {
      return $definition[$key] ?? NULL;
    }
    if (is_object($definition)) {
      if (method_exists($definition, 'get')) {
        $value = $definition->get($key);
        if ($value !== NULL) {
          return $value;
        }
      }
      if ($method !== '' && method_exists($definition, $method)) {
        return $definition->{$method}();
      }
    }
  }
  catch (\Throwable $e) {
  }
  return NULL;
};

try {
  $ids = [];
  $container = \Drupal::getContainer();
  if (method_exists($container, 'getServiceIds')) {
    $ids = $container->getServiceIds();
  }
  if ($__g_def !== NULL) {
    $ids = array_merge($ids, array_keys($__g_def['services'] ?? []), array_keys($__g_def['aliases'] ?? []));
  }
  $ids = array_unique(array_filter($ids, fn ($id) => is_string($id) && str_starts_with($id, 'plugin.manager.')));
  sort($ids);
  foreach ($ids as $id) {
    try {
      $manager = \Drupal::service($id);
      if (!$manager instanceof \Drupal\Component\Plugin\PluginManagerInterface) {
        continue;
      }
      $type_id = substr($id, strlen('plugin.manager.'));
      $plugins = [];
      foreach ($manager->getDefinitions() as $plugin_id => $definition) {
        $pid = is_string($plugin_id) ? $plugin_id : $__g_str($__g_get($definition, 'id', 'id'));
        if ($pid === NULL) {
          continue;
        }
        $class = $__g_str($__g_get($definition, 'class', 'getClass'));
        $base = $__g_str($__g_get($definition, 'base_plugin_id'));
        if ($base === NULL && str_contains($pid, ':')) {
          $base = explode(':', $pid, 2)[0];
        }
        $plugins[] = [
          'id' => $pid,
          'class' => $class,
          'file' => $__g_class_file($class),
          'provider' => $__g_str($__g_get($definition, 'provider', 'getProvider')),
          'deriver' => $__g_str($__g_get($definition, 'deriver', 'getDeriver')),
          'base_plugin_id' => $base,
        ];
      }
      $__g['plugins'][$type_id] = $plugins;
    }
    catch (\Throwable $e) {
      $__g['errors'][] = [
        'source' => 'plugins',
        'class' => get_class($e),
        'message' => $id . ': ' . $e->getMessage(),
      ];
    }
  }
}
catch (\Throwable $e) {
  $__g_error('plugins', $e);
}

// ---- subscribers ----------------------------------------------------------

try {
  $dispatcher = \Drupal::service('event_dispatcher');
  foreach ($dispatcher->getListeners() as $event => $listeners) {
    foreach ($listeners as $listener) {
      $class = NULL;
      if (is_array($listener) && isset($listener[0], $listener[1]) && is_string($listener[1])) {
        if (is_object($listener[0]) && !$listener[0] instanceof \Closure) {
          $class = get_class($listener[0]);
        }
        elseif (is_string($listener[0])) {
          $class = $listener[0];
        }
      }
      if ($class === NULL) {
        $__g['closures'][$event] = ($__g['closures'][$event] ?? 0) + 1;
        continue;
      }
      $callable = $class . '::' . $listener[1];
      $__g['subscribers'][$event][] = [
        'callable' => $callable,
        // The subscriber is the class: an inherited listener's method lives
        // in its base class's file (RouteSubscriberBase), the class does not.
        'file' => $__g_class_file($class),
        'priority' => $dispatcher->getListenerPriority($event, $listener),
      ];
    }
  }
}
catch (\Throwable $e) {
  $__g_error('subscribers', $e);
}

// ---- site -----------------------------------------------------------------

try {
  $drupal_root = defined('DRUPAL_ROOT') ? DRUPAL_ROOT : NULL;
  $composer_root = NULL;
  for ($dir = $drupal_root; is_string($dir) && $dir !== ''; $dir = $parent) {
    if (is_file($dir . '/composer.json')) {
      $composer_root = $dir;
      break;
    }
    $parent = dirname($dir);
    if ($parent === $dir) {
      break;
    }
  }
  sort($__g_enabled);
  $__g['site'] = [
    'drupal_root' => $drupal_root,
    'composer_root' => $composer_root,
    'enabled_extensions_sha' => $__g_seen ? hash('sha256', implode("\n", $__g_enabled)) : NULL,
    'drupal_version' => NULL,
  ];
  $__g['site']['drupal_version'] = \Drupal::VERSION;
}
catch (\Throwable $e) {
  $__g_error('site', $e);
}

foreach (['aliases', 'hooks', 'plugins', 'subscribers', 'closures', 'site'] as $__g_key) {
  $__g[$__g_key] = (object) $__g[$__g_key];
}

ob_end_clean();
echo json_encode($__g, JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR);
