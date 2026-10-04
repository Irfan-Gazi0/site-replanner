using Unity.Robotics.ROSTCPConnector;
using RosMessageTypes.Std;
using UnityEngine;

namespace DataHall {

// /twin/state every 1 s of real time, and /twin/events on a fault.
//
// The two publishers live together because their order matters: a fault
// publishes the event and then a state that already shows the fault, so the
// bridge never replans against a world in which the dead resource is still
// working (same reason as fake_twin's `_fault`).
public class TwinPublisher : MonoBehaviour {

    public static TwinPublisher Instance { get; private set; }

    public float statePeriodSeconds = 1f;

    ROSConnection ros;
    float sinceLastState;

    void Awake() {
        Instance = this;
    }

    void Start() {
        ros = ROSConnection.GetOrCreateInstance();
        ros.RegisterPublisher<StringMsg>("/twin/state");
        ros.RegisterPublisher<StringMsg>("/twin/events");
        PublishState();                       // first state goes out at once
    }

    void Update() {
        sinceLastState += Time.deltaTime;
        if (sinceLastState >= statePeriodSeconds) PublishState();
    }

    public void PublishState() {
        sinceLastState = 0f;
        if (ros == null || TaskBoard.Instance == null) return;
        ros.Publish("/twin/state", new StringMsg(JsonUtility.ToJson(TaskBoard.Instance.BuildState())));
    }

    public void PublishFault(string resourceId) {
        TwinEvent ev = new TwinEvent {
            type = "resource_fault",
            resource = resourceId,
            t = Mathf.Round(TaskBoard.Instance.SimMinutes * 10f) / 10f,
        };
        ros.Publish("/twin/events", new StringMsg(JsonUtility.ToJson(ev)));
        PublishState();
    }
}
}
