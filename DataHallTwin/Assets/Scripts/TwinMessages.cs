using System;

// Wire and data-file shapes for the twin (spec 3.1 and 3.3).
//
// Unity's JsonUtility cannot read dictionaries, top-level arrays or null, so
// every shape here is a class with public fields, every collection is a nested
// array, and "none" is the empty string. Field names match the JSON exactly.
namespace DataHall {

    // ---------------------------------------------------------------- 3.1 ---
    // StreamingAssets/tasks.json, copied from mvp/data/tasks.json by
    // `make sync-unity`. This is the twin's only input file.

    [Serializable]
    public class ZoneSpec {
        public string id;
        public float x;
        public float z;
    }

    [Serializable]
    public class ResourceSpec {
        public string id;
        public string kind;
        public string name;
        public string[] capabilities;
        public float home_x;
        public float home_z;
    }

    [Serializable]
    public class TaskSpec {
        public string id;
        public string name;
        public string zone;
        public string capability;
        public int duration;
        public string[] preds;
    }

    [Serializable]
    public class TasksDoc {
        public string time_unit;
        public ZoneSpec[] zones;
        public ResourceSpec[] resources;
        public TaskSpec[] tasks;
    }

    // ---------------------------------------------------------------- 3.3 ---
    // /twin/state, twin -> bridge, every 1 s of real time.

    [Serializable]
    public class TaskState {
        public string id;
        public string status;      // pending | ongoing | completed
        public string resource;    // "" while pending
        public int start;
        public int end;
    }

    [Serializable]
    public class ResourceState {
        public string id;
        public string status;      // idle | busy | fault
        public string task;        // "" when idle or faulted
    }

    [Serializable]
    public class TwinState {
        public float t;
        public TaskState[] tasks;
        public ResourceState[] resources;
    }

    // /twin/events, twin -> bridge, on a fault button click.
    [Serializable]
    public class TwinEvent {
        public string type;        // resource_fault
        public string resource;
        public float t;
    }

    // /plan/assignments, bridge -> twin, after approval. Non-completed tasks
    // only; `end - start` is the duration the twin must use.
    [Serializable]
    public class Assignment {
        public string task;
        public string resource;
        public int start;
        public int end;
        public string zone;
        public string[] preds;
    }

    [Serializable]
    public class PlanMsg {
        public int plan_id;
        public int t_now;
        public Assignment[] assignments;
    }

    // /plan/status, bridge -> twin, on every state change.
    [Serializable]
    public class PlanStatus {
        public int plan_id;
        public string state;       // replanning | awaiting_approval | dispatched | escalated | rejected
        public string message;
    }
}
