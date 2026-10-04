using Unity.Robotics.ROSTCPConnector;
using RosMessageTypes.Std;
using UnityEngine;

namespace DataHall {

// /plan/assignments, bridge -> twin. A plan replaces every queue (spec 3.4),
// so the whole message is handed to TaskBoard, which applies it in one go.
public class PlanSubscriber : MonoBehaviour {

    void Start() {
        ROSConnection.GetOrCreateInstance().Subscribe<StringMsg>("/plan/assignments", OnPlan);
    }

    void OnPlan(StringMsg msg) {
        PlanMsg plan = JsonUtility.FromJson<PlanMsg>(msg.data);
        if (plan == null) {
            Debug.LogError("PlanSubscriber: could not parse /plan/assignments: " + msg.data);
            return;
        }
        TaskBoard.Instance.ApplyPlan(plan);
    }
}
}
