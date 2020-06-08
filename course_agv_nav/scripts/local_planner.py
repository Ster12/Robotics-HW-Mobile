#!/usr/bin/env python
# -*- coding: utf-8 -*-
import numpy as np
import rospy
import tf
from nav_msgs.msg import Path
from geometry_msgs.msg import PoseStamped,Twist
from sensor_msgs.msg import LaserScan

from dwa import *

from threading import Lock,Thread
import time

from course_agv_nav.srv import Plan, PlanResponse

def limitVal(minV,maxV,v):
    if v < minV:
        return minV
    if v > maxV:
        return maxV
    return v

class LocalPlanner:
    def __init__(self):
        self.arrive = 0.2
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self.vx = 0.0
        self.vw = 0.0
        # init plan_config for once
        self.plan_config = Config()
        self.plan_config.robot_type = RobotType.rectangle
        c = self.plan_config
        self.threshold = c.max_speed*c.predict_time

        self.laser_lock = Lock()
        self.lock = Lock()
        self.path = Path()
        self.tf = tf.TransformListener()
        self.path_sub = rospy.Subscriber('/course_agv/global_path',Path,self.pathCallback)
        self.vel_pub = rospy.Publisher('/course_agv/velocity',Twist, queue_size=1)
        self.midpose_pub = rospy.Publisher('/course_agv/mid_goal',PoseStamped,queue_size=1)
        self.laser_sub = rospy.Subscriber('/course_agv/laser/scan',LaserScan,self.laserCallback)
        self.planner_thread = None

        self.flag = True
        self.dwa_cnt = 0
        self.dwa_maxtime = 50
        self.exit_flag = True
        self.replan_client = rospy.ServiceProxy('/course_agv/global_plan',Plan)

    def updateGlobalPose(self):
        try:
            self.tf.waitForTransform("/map", "/robot_base", rospy.Time(), rospy.Duration(4.0))
            (self.trans,self.rot) = self.tf.lookupTransform('/map','/robot_base',rospy.Time(0))
        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            print("get tf error!")
        euler = tf.transformations.euler_from_quaternion(self.rot)
        roll,pitch,yaw = euler[0],euler[1],euler[2]
        self.x = self.trans[0]
        self.y = self.trans[1]
        self.yaw = yaw
        ind = self.goal_index
        

        while ind < len(self.path.poses):
            p = self.path.poses[ind].pose.position
            dis = math.hypot(p.x-self.x,p.y-self.y)
            # print(ind)
            if dis < self.threshold:
                self.goal_index = ind
            else:
                self.goal_index = ind
                break
            ind += 1
        goal = self.path.poses[self.goal_index]

        self.midpose_pub.publish(goal)
        lgoal = self.tf.transformPose("/robot_base", goal)
        self.plan_goal = np.array([lgoal.pose.position.x,lgoal.pose.position.y])
        self.goal_dis = math.hypot(self.x-self.path.poses[-1].pose.position.x,self.y-self.path.poses[-1].pose.position.y)

    def laserCallback(self,msg):
        self.laser_lock.acquire()
        # preprocess
        self.ob = [[100,100]]
        angle_min = msg.angle_min
        angle_increment = msg.angle_increment
        for i in range(len(msg.ranges)):
            a = angle_min + angle_increment*i
            r = msg.ranges[i]
            if r < self.threshold:
                self.ob.append([math.cos(a)*r,math.sin(a)*r])
        self.laser_lock.release()

    def updateObstacle(self):
        self.laser_lock.acquire()
        self.plan_ob = []
        self.plan_ob = np.array(self.ob)
        self.laser_lock.release()

    def pathCallback(self,msg):
        self.path = msg
        self.lock.acquire()
        self.initPlanning()
        self.lock.release()

        while self.planner_thread != None:
            pass
        self.planner_thread = Thread(target=self.planThreadFunc)
        self.planner_thread.start()     

    def initPlanning(self):
        self.goal_index = 0
        self.goal_index_last = 0
        if self.flag:
            self.vx = 0.0
            self.vw = 0.0
            self.flag = False
        self.updateGlobalPose()
        cx = []
        cy = []
        for pose in self.path.poses:
            cx.append(pose.pose.position.x)
            cy.append(pose.pose.position.y)
        self.goal = np.array([cx[0],cy[0]])
        self.plan_cx,self.plan_cy = np.array(cx),np.array(cy)
        self.plan_goal = np.array([cx[-1],cy[-1]])
        self.plan_x = np.array([0.0,0.0,0.0,self.vx,self.vw])

    def planThreadFunc(self):
        print("running planning thread!!")
        while True:
            self.lock.acquire()
            self.planOnce()
            self.lock.release()
            if self.goal_dis < self.arrive:
                self.exit_flag = True
                print("arrive goal!")
                break

            if self.goal_index - self.goal_index_last > 3:
                self.dwa_cnt = 0
                self.goal_index_last = self.goal_index


            if self.dwa_cnt > self.dwa_maxtime:
                self.dwa_cnt = 0
                try:
                    beforetime = rospy.get_time()
                    self.replan_client.call()
                    nowtime = rospy.get_time()
                    print("replan, Time: ", nowtime - beforetime)
                    self.exit_flag = False
                    break
                except rospy.ServiceException, e:
                    break

            time.sleep(0.001)
        print("exit planning thread!!")
        self.lock.acquire()
        if self.exit_flag:
            self.publishVel(True)
        else:
            self.publishVel()
        self.lock.release()
        self.planner_thread = None

    def planOnce(self):
        self.dwa_cnt += 1
        print("dwa_cnt:",self.dwa_cnt)
        self.updateGlobalPose()

        self.plan_x = [0.0,0.0,0.0,self.vx,self.vw]

        self.updateObstacle()
        u, _ = dwa_control(self.plan_x,self.plan_config,self.plan_goal,self.plan_ob)
        alpha = 0.7 #0.5
        self.vx = u[0]*alpha+self.vx*(1-alpha)
        self.vw = u[1]*alpha+self.vw*(1-alpha)
        self.publishVel()

    def publishVel(self,zero = False):
        if zero:
            self.vx = 0
            self.vw = 0
        cmd = Twist()
        cmd.linear.x = self.vx
        cmd.angular.z = self.vw
        #print("vel_pub.publish")
        self.vel_pub.publish(cmd)

def main():
    rospy.init_node('path_Planning')
    lp = LocalPlanner()
    rospy.spin()

if __name__ == '__main__':
    main()
