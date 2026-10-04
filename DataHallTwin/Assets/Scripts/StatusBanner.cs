using Unity.Robotics.ROSTCPConnector;
using RosMessageTypes.Std;
using UnityEngine;
using TMPro;

namespace DataHall {

// /plan/status on the banner. The colour is what the video reads at a glance:
// blue replanning, amber awaiting approval, green dispatched, red escalated.
public class StatusBanner : MonoBehaviour {

    public TMP_Text banner;

    static readonly Color Blue  = new Color(0.25f, 0.55f, 0.95f);
    static readonly Color Amber = new Color(0.95f, 0.70f, 0.15f);
    static readonly Color Green = new Color(0.25f, 0.75f, 0.35f);
    static readonly Color Red   = new Color(0.90f, 0.25f, 0.25f);
    static readonly Color Grey  = new Color(0.70f, 0.70f, 0.70f);

    void Start() {
        ROSConnection.GetOrCreateInstance().Subscribe<StringMsg>("/plan/status", OnStatus);
        if (banner != null) banner.text = "waiting for the bridge";
    }

    void OnStatus(StringMsg msg) {
        PlanStatus st = JsonUtility.FromJson<PlanStatus>(msg.data);
        if (st == null) {
            Debug.LogError("StatusBanner: could not parse /plan/status: " + msg.data);
            return;
        }
        Debug.Log("status: " + st.state + " - " + st.message);
        if (banner == null) return;
        banner.text = st.state + ": " + st.message;
        banner.color = ColorFor(st.state);
    }

    static Color ColorFor(string state) {
        switch (state) {
            case "replanning":        return Blue;
            case "awaiting_approval": return Amber;
            case "dispatched":        return Green;
            case "escalated":
            case "rejected":          return Red;
            default:                  return Grey;
        }
    }
}
}
